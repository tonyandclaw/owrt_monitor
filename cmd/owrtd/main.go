// owrtd is the HTTP-facing companion to the Python orchestrator.
//
// The Python `BuildWorkflow` writes per-job state to disk under
// `project.artifact_dir`, including `report.json` (final summary) and
// `events.jsonl` (per-event stream). owrtd surfaces those files over HTTP
// without re-implementing the workflow engine. The first write-side slice
// accepts job submissions and launches the Python CLI as a supervised child
// process; Python remains the workflow engine until Phase 7 fully migrates
// execution into Go.
//
// Endpoints today:
//
//	GET  /healthz                          → {"status": "ok"}
//	GET  /ui/                              → job dashboard
//	GET  /v1/jobs?limit=N                   → [{job_id, ...}, ...]   (newest first)
//	GET  /v1/jobs/{id}                      → full report.json
//	DELETE /v1/jobs/{id}                    → remove the on-disk job directory
//	GET  /v1/jobs/{id}/analysis             → advisory analysis.json
//	GET  /v1/jobs/{id}/events               → raw events.jsonl bytes
//	GET  /v1/jobs/{id}/runner               → Go runner child-process status
//	GET  /v1/jobs/{id}/runner-output        → structured stdout/stderr NDJSON
//	GET  /v1/jobs/{id}/files/<path>         → serve files from the run_dir
//	POST /v1/jobs/{id}/cancel               → write cancel.flag marker
//	GET  /v1/locks                          → current DUT + builder locks
//	POST /v1/locks/{dut|builder|serial|artifact}/{name}/acquire
//	POST /v1/locks/{dut|builder|serial|artifact}/{name}/heartbeat
//	POST /v1/locks/{dut|builder|serial|artifact}/{name}/release
//	POST /v1/jobs                           → launch a Python workflow subprocess
//
// Storage of truth is the on-disk run directories. SQLite is intentionally
// not opened from Go: it would add a cgo or pure-Go dep we don't need until
// the write-side actually requires consistent reads.
package main

import (
	"flag"
	"log"
	"net/http"
	"path/filepath"
	"time"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8765", "HTTP listen address")
	root := flag.String("artifacts-dir", "./artifacts",
		"path that contains the per-job run directories (Python's project.artifact_dir)")
	runnerBin := flag.String("owrt-monitor-bin", "owrt-monitor",
		"owrt-monitor executable used by POST /v1/jobs")
	runnerOutputMaxBytes := flag.Int64("runner-output-max-bytes", 64*1024*1024,
		"maximum bytes written to runner.log and runner.output.jsonl per job before truncating output; <=0 disables")
	runnerOutputRotateBytes := flag.Int64("runner-output-rotate-bytes", 16*1024*1024,
		"maximum bytes kept in the active runner.log and runner.output.jsonl files before rotating to .1; <=0 disables")
	runnerOutputRotateFiles := flag.Int("runner-output-rotate-files", 3,
		"number of rotated runner output files to keep when rotation is enabled; <=0 keeps 1")
	flag.Parse()

	abs, err := filepath.Abs(*root)
	if err != nil {
		log.Fatalf("resolve artifacts-dir: %v", err)
	}

	srv := &server{
		artifactsDir:            abs,
		runnerBin:               *runnerBin,
		runnerOutputMaxBytes:    *runnerOutputMaxBytes,
		runnerOutputRotateBytes: *runnerOutputRotateBytes,
		runnerOutputRotateFiles: *runnerOutputRotateFiles,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.handleHealthz)
	mux.HandleFunc("/", srv.handleDashboard)
	mux.HandleFunc("/v1/jobs", srv.handleJobs)
	// `/v1/jobs/{id}` and its sub-resources are dispatched by handleJobByID.
	mux.HandleFunc("/v1/jobs/", srv.handleJobByID)
	mux.HandleFunc("/v1/locks", srv.handleLocks)
	mux.HandleFunc("/v1/locks/", srv.handleLockByID)

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
