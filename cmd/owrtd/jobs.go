package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

func (s *server) handleJobs(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost {
		s.submitJob(w, r)
		return
	}
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET or POST only"})
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

func (s *server) submitJob(w http.ResponseWriter, r *http.Request) {
	var req jobSubmitRequest
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: fmt.Sprintf("invalid JSON: %v", err)})
		return
	}
	args, err := req.cliArgs()
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: err.Error()})
		return
	}
	jobID, err := newRunnerJobID()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	runDir := filepath.Join(s.artifactsDir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("create run dir: %v", err),
		})
		return
	}

	logPath := filepath.Join(runDir, "runner.log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("open runner log: %v", err),
		})
		return
	}
	outputPath := filepath.Join(runDir, "runner.output.jsonl")
	outputFile, err := os.OpenFile(outputPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		_ = logFile.Close()
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("open runner output log: %v", err),
		})
		return
	}

	runnerBin := s.runnerBin
	if runnerBin == "" {
		runnerBin = "owrt-monitor"
	}
	fullCommand := append([]string{runnerBin}, args...)
	statusPath := filepath.Join(runDir, "runner.json")
	now := time.Now().UTC().Format(time.RFC3339Nano)
	status := runnerStatus{
		JobID:        jobID,
		Status:       "starting",
		Command:      fullCommand,
		RunDir:       runDir,
		RunnerLog:    logPath,
		RunnerOutput: outputPath,
		StartedAt:    now,
		UpdatedAt:    now,
	}
	if err := writeRunnerStatus(statusPath, status); err != nil {
		_ = outputFile.Close()
		_ = logFile.Close()
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("write runner status: %v", err),
		})
		return
	}

	cmd := exec.Command(runnerBin, args...) // #nosec G204 -- argv is validated and never shell-expanded.
	cmd.Env = append(os.Environ(), "OWRT_MONITOR_JOB_ID="+jobID)
	if req.WorkingDir != "" {
		cmd.Dir = req.WorkingDir
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		_ = outputFile.Close()
		_ = logFile.Close()
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("create stdout pipe: %v", err),
		})
		return
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		_ = stdoutReader.Close()
		_ = stdoutWriter.Close()
		_ = outputFile.Close()
		_ = logFile.Close()
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("create stderr pipe: %v", err),
		})
		return
	}
	cmd.Stdout = stdoutWriter
	cmd.Stderr = stderrWriter
	if err := cmd.Start(); err != nil {
		_ = stdoutReader.Close()
		_ = stdoutWriter.Close()
		_ = stderrReader.Close()
		_ = stderrWriter.Close()
		_ = outputFile.Close()
		_ = logFile.Close()
		finishedAt := time.Now().UTC().Format(time.RFC3339Nano)
		status.Status = "start_failed"
		status.UpdatedAt = finishedAt
		status.FinishedAt = finishedAt
		status.Error = err.Error()
		if writeErr := writeRunnerStatus(statusPath, status); writeErr != nil {
			log.Printf("runner job %s status write after start failure: %v", jobID, writeErr)
		}
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("start owrt-monitor: %v", err),
		})
		return
	}
	_ = stdoutWriter.Close()
	_ = stderrWriter.Close()
	status.Status = "running"
	status.PID = cmd.Process.Pid
	status.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	if err := writeRunnerStatus(statusPath, status); err != nil {
		log.Printf("runner job %s status write after start: %v", jobID, err)
	}
	outputWriter := &runnerOutputWriter{
		jobID:          jobID,
		statusPath:     statusPath,
		humanPath:      logPath,
		structuredPath: outputPath,
		human:          logFile,
		structured:     outputFile,
		maxBytes:       s.runnerOutputLimit(),
		rotateBytes:    s.runnerOutputRotateLimit(),
		rotateFiles:    s.runnerOutputRotateFileCount(),
	}
	outputDone := make(chan error, 2)
	go captureRunnerOutput(outputWriter, "stdout", stdoutReader, outputDone)
	go captureRunnerOutput(outputWriter, "stderr", stderrReader, outputDone)
	done := make(chan error, 1)
	go func() {
		err := cmd.Wait()
		for i := 0; i < 2; i++ {
			if outputErr := <-outputDone; outputErr != nil {
				log.Printf("runner job %s output capture: %v", jobID, outputErr)
			}
		}
		if outputWriter.isTruncated() {
			if truncErr := markRunnerOutputTruncated(statusPath); truncErr != nil {
				log.Printf("runner job %s output truncation status update after capture: %v", jobID, truncErr)
			}
		}
		if outputWriter.isRotated() {
			if rotateErr := markRunnerOutputRotated(statusPath); rotateErr != nil {
				log.Printf("runner job %s output rotation status update after capture: %v", jobID, rotateErr)
			}
		}
		if closeErr := outputWriter.close(); closeErr != nil {
			log.Printf("runner job %s output close: %v", jobID, closeErr)
		}
		done <- err
	}()
	go s.monitorRunner(jobID, statusPath, status, cmd, done)

	writeJSON(w, http.StatusAccepted, jobSubmitResponse{
		JobID:        jobID,
		PID:          cmd.Process.Pid,
		Status:       "accepted",
		Command:      fullCommand,
		RunDir:       runDir,
		RunnerLog:    logPath,
		RunnerOutput: outputPath,
	})
}

func (s *server) handleJobByID(w http.ResponseWriter, r *http.Request) {
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
		// Bare /v1/jobs/{id} — read or remove one job.
		switch r.Method {
		case http.MethodGet:
			s.serveReport(w, jobID)
		case http.MethodDelete:
			s.deleteJob(w, jobID)
		default:
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET or DELETE only"})
		}
		return
	}

	// Some sub-resources (e.g. files/<path>) keep nested path components.
	subhead, subtail, hasSubtail := strings.Cut(parts[1], "/")
	switch subhead {
	case "analysis":
		if hasSubtail {
			writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such sub-resource"})
			return
		}
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
			return
		}
		s.serveAnalysis(w, jobID)
	case "events":
		if hasSubtail {
			writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such sub-resource"})
			return
		}
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
			return
		}
		s.serveEvents(w, jobID)
	case "runner":
		if hasSubtail {
			writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such sub-resource"})
			return
		}
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
			return
		}
		s.serveRunner(w, jobID)
	case "runner-output":
		if hasSubtail {
			writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such sub-resource"})
			return
		}
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
			return
		}
		s.serveRunnerOutput(w, r, jobID)
	case "cancel":
		if hasSubtail {
			writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such sub-resource"})
			return
		}
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "POST only"})
			return
		}
		s.serveCancel(w, jobID)
	case "files":
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
			return
		}
		s.serveFiles(w, r, jobID, subtail)
	default:
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such sub-resource"})
	}
}

func (s *server) deleteJob(w http.ResponseWriter, jobID string) {
	runDir, err := s.jobRunDir(jobID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: err.Error()})
		return
	}
	info, err := os.Stat(runDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such job directory"})
			return
		}
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	if !info.IsDir() {
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "no such job directory"})
		return
	}
	if reason, err := s.jobDeletionBlocker(jobID); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	} else if reason != "" {
		writeJSON(w, http.StatusConflict, errorResponse{Error: "cannot remove job: " + reason})
		return
	}
	if err := os.RemoveAll(runDir); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: fmt.Sprintf("remove job directory: %v", err)})
		return
	}
	writeJSON(w, http.StatusOK, jobDeleteResponse{
		JobID:   jobID,
		Removed: true,
		RunDir:  runDir,
	})
}

func (s *server) jobRunDir(jobID string) (string, error) {
	if !isSafeJobID(jobID) {
		return "", errors.New("job id must contain only alphanumerics, underscore, or hyphen")
	}
	root, err := filepath.Abs(s.artifactsDir)
	if err != nil {
		return "", fmt.Errorf("resolve artifacts dir: %w", err)
	}
	runDir, err := filepath.Abs(filepath.Join(root, jobID))
	if err != nil {
		return "", fmt.Errorf("resolve job directory: %w", err)
	}
	rel, err := filepath.Rel(root, runDir)
	if err != nil {
		return "", fmt.Errorf("validate job directory: %w", err)
	}
	if rel == "." || rel == ".." || filepath.IsAbs(rel) ||
		strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", errors.New("job directory escaped artifacts dir")
	}
	return runDir, nil
}

func (s *server) jobDeletionBlocker(jobID string) (string, error) {
	runDir, err := s.jobRunDir(jobID)
	if err != nil {
		return "", err
	}
	statusPath := filepath.Join(runDir, "runner.json")
	if status, ok := readRunnerStatusBestEffort(statusPath); ok {
		status = s.reconcileRunnerStatus(statusPath, status)
		if runnerStatusMayBeActive(status.Status) {
			return "runner is still active", nil
		}
	}
	snapshot, err := s.readLocksSnapshot()
	if err != nil {
		return "", fmt.Errorf("read locks: %w", err)
	}
	if reason, ok := lockOwnedByJob(snapshot, jobID); ok {
		return reason, nil
	}
	return "", nil
}

func lockOwnedByJob(snapshot locksSnapshot, jobID string) (string, bool) {
	for _, lock := range snapshot.DutLocks {
		if lock.OwnerJobID == jobID {
			return fmt.Sprintf("job owns DUT lock %q", lock.DutName), true
		}
	}
	for _, lock := range snapshot.BuilderLocks {
		if lock.OwnerJobID == jobID {
			return fmt.Sprintf("job owns builder lock %q", lock.BuilderName), true
		}
	}
	for _, lock := range snapshot.SerialLocks {
		if lock.OwnerJobID == jobID {
			return fmt.Sprintf("job owns serial lock %q", lock.Name), true
		}
	}
	for _, lock := range snapshot.ArtifactLocks {
		if lock.OwnerJobID == jobID {
			return fmt.Sprintf("job owns artifact lock %q", lock.Name), true
		}
	}
	return "", false
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

func (s *server) serveFiles(w http.ResponseWriter, r *http.Request, jobID, subPath string) {
	// http.FileServer + http.Dir is the standard way to serve a sandboxed
	// directory tree. http.Dir.Open rejects requests that would escape the
	// root via `..` segments — we still validate the job dir exists first
	// to return a clean 404 instead of leaking that detail through the
	// FileServer error.
	jobDir := filepath.Join(s.artifactsDir, jobID)
	info, err := os.Stat(jobDir)
	if err != nil || !info.IsDir() {
		writeJSON(w, http.StatusNotFound, errorResponse{
			Error: "no such job (run directory does not exist)",
		})
		return
	}
	if subPath == "" {
		// Treat /v1/jobs/{id}/files (no trailing path) as a directory listing.
		subPath = "/"
	}
	// Re-shape the request URL so http.FileServer sees the path scoped
	// inside the job dir. We don't mutate the original request — make a
	// shallow copy with rewritten URL.path.
	scoped := *r
	urlCopy := *r.URL
	urlCopy.Path = "/" + strings.TrimPrefix(subPath, "/")
	scoped.URL = &urlCopy
	http.FileServer(http.Dir(jobDir)).ServeHTTP(w, &scoped)
}

func (s *server) serveCancel(w http.ResponseWriter, jobID string) {
	// The Python orchestrator polls `<run_dir>/cancel.flag` between every
	// step. Writing the file is equivalent to `owrt-monitor cancel <id>` —
	// no daemon-internal state needed, no new IPC mechanism.
	jobDir := filepath.Join(s.artifactsDir, jobID)
	info, err := os.Stat(jobDir)
	if err != nil || !info.IsDir() {
		writeJSON(w, http.StatusNotFound, errorResponse{
			Error: "no such job (run directory does not exist)",
		})
		return
	}
	marker := filepath.Join(jobDir, "cancel.flag")
	if err := os.WriteFile(marker, []byte("requested\n"), 0o644); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("write cancel marker: %v", err),
		})
		return
	}
	if err := s.markRunnerCancelRequested(jobID); err != nil {
		log.Printf("runner job %s cancellation status update: %v", jobID, err)
	}
	writeJSON(w, http.StatusAccepted, map[string]any{
		"job_id":      jobID,
		"marker_path": marker,
		"status":      "cancellation requested",
	})
}

func (s *server) markRunnerCancelRequested(jobID string) error {
	path := filepath.Join(s.artifactsDir, jobID, "runner.json")
	status, ok := readRunnerStatusBestEffort(path)
	if !ok {
		return nil
	}
	if status.Status == "exited" || status.Status == "start_failed" {
		return nil
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	status.Status = "cancel_requested"
	status.CancelRequestedAt = now
	status.UpdatedAt = now
	return writeRunnerStatus(path, status)
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
		return out[i].sortKey > out[j].sortKey
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
	sortKey := partial.StartedAt
	if sortKey == "" {
		if info, err := os.Stat(path); err == nil {
			sortKey = info.ModTime().UTC().Format(time.RFC3339Nano)
		}
	}
	return jobsListEntry{
		JobID:      partial.JobID,
		State:      partial.State,
		Success:    partial.Success,
		DryRun:     partial.DryRun,
		StartedAt:  partial.StartedAt,
		FinishedAt: partial.FinishedAt,
		RunDir:     partial.RunDir,
		sortKey:    sortKey,
	}, true
}

func (r jobSubmitRequest) cliArgs() ([]string, error) {
	command := strings.TrimSpace(r.Command)
	config := strings.TrimSpace(r.Config)
	if command == "" {
		return nil, errors.New("command is required")
	}
	if config == "" {
		return nil, errors.New("config is required")
	}

	addCommon := func(args []string) []string {
		args = append(args, "--config", config)
		if strings.TrimSpace(r.Profile) != "" {
			args = append(args, "--profile", strings.TrimSpace(r.Profile))
		}
		if r.DryRun {
			args = append(args, "--dry-run")
		}
		return args
	}

	switch command {
	case "build":
		if r.AllowFlash {
			return nil, errors.New("command build does not accept allow_flash")
		}
		if strings.TrimSpace(r.Artifact) != "" {
			return nil, errors.New("command build does not accept artifact")
		}
		return addCommon([]string{"build"}), nil
	case "run":
		if strings.TrimSpace(r.Artifact) != "" {
			return nil, errors.New("command run does not accept artifact")
		}
		args := addCommon([]string{"run"})
		if r.AllowFlash {
			args = append(args, "--allow-flash")
		}
		return args, nil
	case "flash":
		artifact := strings.TrimSpace(r.Artifact)
		if artifact == "" {
			return nil, errors.New("artifact is required for command flash")
		}
		if !r.DryRun && !r.AllowFlash {
			return nil, errors.New("command flash requires allow_flash unless dry_run is true")
		}
		args := []string{"flash", "--artifact", artifact}
		args = addCommon(args)
		if r.AllowFlash {
			args = append(args, "--allow-flash")
		}
		return args, nil
	case "test":
		if r.AllowFlash {
			return nil, errors.New("command test does not accept allow_flash")
		}
		if strings.TrimSpace(r.Artifact) != "" {
			return nil, errors.New("command test does not accept artifact")
		}
		return addCommon([]string{"test"}), nil
	default:
		return nil, fmt.Errorf("unsupported command %q", command)
	}
}

func newRunnerJobID() (string, error) {
	var buf [6]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return "", fmt.Errorf("generate job id: %w", err)
	}
	return "job_" + hex.EncodeToString(buf[:]), nil
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
