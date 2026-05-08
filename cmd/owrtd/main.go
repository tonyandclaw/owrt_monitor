// owrtd is the read-only HTTP-facing companion to the Python orchestrator.
//
// The Python `BuildWorkflow` writes per-job state to disk under
// `project.artifact_dir`, including `report.json` (final summary) and
// `events.jsonl` (per-event stream). owrtd surfaces those files over HTTP
// without re-implementing the workflow engine — Phase 7 of the roadmap
// reserves the write-side (submit/cancel/lock) for a later milestone.
//
// Endpoints today:
//   GET /healthz                       → {"status": "ok"}
//   GET /v1/jobs?limit=N                → [{job_id, ...}, ...]   (newest first)
//   GET /v1/jobs/{id}                   → full report.json
//   GET /v1/jobs/{id}/events            → raw events.jsonl bytes
//   GET /v1/jobs                        → 501 stub if unauthorised mutation
//
// Storage of truth is the on-disk run directories. SQLite is intentionally
// not opened from Go: it would add a cgo or pure-Go dep we don't need until
// the write-side actually requires consistent reads.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

type healthResponse struct {
	Status string `json:"status"`
}

type errorResponse struct {
	Error string `json:"error"`
}

// jobsListEntry is the trimmed shape returned by /v1/jobs. Mirrors the
// most-useful fields from the Python `report.json` so a UI can render a
// list without reading every report.
type jobsListEntry struct {
	JobID      string `json:"job_id"`
	State      string `json:"state"`
	Success    bool   `json:"success"`
	DryRun     bool   `json:"dry_run"`
	StartedAt  string `json:"started_at,omitempty"`
	FinishedAt string `json:"finished_at,omitempty"`
	RunDir     string `json:"run_dir"`
}

type server struct {
	artifactsDir string
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8765", "HTTP listen address")
	root := flag.String("artifacts-dir", "./artifacts",
		"path that contains the per-job run directories (Python's project.artifact_dir)")
	flag.Parse()

	abs, err := filepath.Abs(*root)
	if err != nil {
		log.Fatalf("resolve artifacts-dir: %v", err)
	}

	srv := &server{artifactsDir: abs}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.handleHealthz)
	mux.HandleFunc("/v1/jobs", srv.handleJobs)
	// `/v1/jobs/{id}` and `/v1/jobs/{id}/events` are dispatched by handleJobByID.
	mux.HandleFunc("/v1/jobs/", srv.handleJobByID)

	// owrtd is intended for localhost-only access (default 127.0.0.1) and
	// reads from the same machine's filesystem; it does not handle traffic
	// that warrants TLS. If exposed beyond loopback, front it with a
	// reverse proxy (nginx/caddy) that terminates TLS.
	srvHTTP := &http.Server{
		Addr:              *addr,
		Handler:           mux,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("owrtd listening on http://%s, artifacts=%s", *addr, abs)
	if err := srvHTTP.ListenAndServe(); err != nil { // nosemgrep: go.lang.security.audit.net.use-tls.use-tls
		log.Fatal(err)
	}
}

func (s *server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, healthResponse{Status: "ok"})
}

func (s *server) handleJobs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		// Mutation API still reserved for a later milestone; preserve the
		// historical 501 so Python's fallback decision logic can detect it.
		writeJSON(w, http.StatusNotImplemented, errorResponse{
			Error: "owrtd job mutation API is reserved for a later runner milestone",
		})
		return
	}

	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 1000 {
			writeJSON(w, http.StatusBadRequest, errorResponse{
				Error: "limit must be an integer in [1, 1000]",
			})
			return
		}
		limit = parsed
	}

	entries, err := s.listJobs(limit)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, entries)
}

// handleJobByID dispatches `/v1/jobs/{id}` and `/v1/jobs/{id}/events`.
func (s *server) handleJobByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
		return
	}

	rest := strings.TrimPrefix(r.URL.Path, "/v1/jobs/")
	if rest == "" {
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "missing job id"})
		return
	}
	parts := strings.SplitN(rest, "/", 2)
	jobID := parts[0]
	if !isSafeJobID(jobID) {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error: "job id must contain only alphanumerics, underscore, or hyphen",
		})
		return
	}

	if len(parts) == 1 {
		s.serveReport(w, jobID)
		return
	}

	switch parts[1] {
	case "events":
		s.serveEvents(w, jobID)
	default:
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such sub-resource"})
	}
}

func (s *server) serveReport(w http.ResponseWriter, jobID string) {
	path := filepath.Join(s.artifactsDir, jobID, "report.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeJSON(w, http.StatusNotFound, errorResponse{
				Error: "no such job (or report.json not yet written)",
			})
			return
		}
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	// Decode into a generic map and re-encode through json.NewEncoder. Two
	// reasons: (a) validates the payload before responding, surfacing a
	// corrupt report.json clearly instead of streaming garbage; (b) routes
	// every byte through the same encoder used by writeJSON, so output is
	// uniformly JSON (no accidental partial-binary leakage from a torn
	// file read).
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("report.json is not valid JSON: %v", err),
		})
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *server) serveEvents(w http.ResponseWriter, jobID string) {
	path := filepath.Join(s.artifactsDir, jobID, "events.jsonl")
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeJSON(w, http.StatusNotFound, errorResponse{
				Error: "no events.jsonl for that job",
			})
			return
		}
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	defer f.Close()
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	if _, err := io.Copy(w, f); err != nil {
		log.Printf("events.jsonl copy for %s: %v", jobID, err)
	}
}

// listJobs walks the artifacts dir, reads each `report.json`, and returns
// up to `limit` entries newest-first by `started_at`. Jobs whose report
// file is missing or malformed are skipped (an in-flight job whose first
// `report.json` write hasn't happened yet still appears in `status` via
// the Python SQLite path; this Go endpoint is best-effort).
func (s *server) listJobs(limit int) ([]jobsListEntry, error) {
	entries, err := os.ReadDir(s.artifactsDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return []jobsListEntry{}, nil
		}
		return nil, fmt.Errorf("read artifacts dir: %w", err)
	}

	var out []jobsListEntry
	for _, dirent := range entries {
		if !dirent.IsDir() {
			continue
		}
		name := dirent.Name()
		if !strings.HasPrefix(name, "job_") {
			continue
		}
		entry, ok := s.readJobEntry(name)
		if !ok {
			continue
		}
		out = append(out, entry)
	}

	sort.Slice(out, func(i, j int) bool {
		return out[i].StartedAt > out[j].StartedAt
	})
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *server) readJobEntry(jobID string) (jobsListEntry, bool) {
	path := filepath.Join(s.artifactsDir, jobID, "report.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return jobsListEntry{}, false
	}
	// Decode just the fields we care about; the report has many others.
	var partial struct {
		JobID      string `json:"job_id"`
		State      string `json:"state"`
		Success    bool   `json:"success"`
		DryRun     bool   `json:"dry_run"`
		RunDir     string `json:"run_dir"`
		StartedAt  string `json:"started_at"`
		FinishedAt string `json:"finished_at"`
	}
	if err := json.Unmarshal(data, &partial); err != nil {
		return jobsListEntry{}, false
	}
	if partial.JobID == "" {
		partial.JobID = jobID
	}
	return jobsListEntry{
		JobID:      partial.JobID,
		State:      partial.State,
		Success:    partial.Success,
		DryRun:     partial.DryRun,
		StartedAt:  partial.StartedAt,
		FinishedAt: partial.FinishedAt,
		RunDir:     partial.RunDir,
	}, true
}

// isSafeJobID guards path construction. Job IDs from Python are
// `job_<12-hex>` so the regex of allowed chars is small. Anything that
// isn't strictly alphanumeric / `_` / `-` is rejected before we touch
// the filesystem — defends against `..` and absolute-path injection.
func isSafeJobID(id string) bool {
	if id == "" {
		return false
	}
	for _, r := range id {
		switch {
		case r >= 'a' && r <= 'z':
		case r >= 'A' && r <= 'Z':
		case r >= '0' && r <= '9':
		case r == '_' || r == '-':
		default:
			return false
		}
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("write response: %v", err)
	}
}
