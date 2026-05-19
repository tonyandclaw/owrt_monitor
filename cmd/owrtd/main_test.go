package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// newTestServer wires up a `server` rooted at a temp dir so API handlers
// have something to chew on without touching the real lab.
func newTestServer(t *testing.T) (*server, string) {
	t.Helper()
	dir := t.TempDir()
	return &server{artifactsDir: dir, runnerBin: "owrt-monitor"}, dir
}

func seedJob(t *testing.T, root, jobID string, report map[string]any, events string) {
	t.Helper()
	dir := filepath.Join(root, jobID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir job: %v", err)
	}
	data, err := json.Marshal(report)
	if err != nil {
		t.Fatalf("marshal report: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "report.json"), data, 0o644); err != nil {
		t.Fatalf("write report: %v", err)
	}
	if events != "" {
		if err := os.WriteFile(filepath.Join(dir, "events.jsonl"), []byte(events), 0o644); err != nil {
			t.Fatalf("write events: %v", err)
		}
	}
}

func TestHealthzReturnsOK(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	srv.handleHealthz(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("want Content-Type application/json, got %q", got)
	}
	var body healthResponse
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Status != "ok" {
		t.Fatalf(`want "ok", got %q`, body.Status)
	}
}

func TestDashboardRedirectsRoot(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	srv.handleDashboard(rec, req)
	if rec.Code != http.StatusFound {
		t.Fatalf("want 302, got %d", rec.Code)
	}
	if got := rec.Header().Get("Location"); got != "/ui/" {
		t.Fatalf("want redirect to /ui/, got %q", got)
	}
}

func TestDashboardServesHTMLAndAssets(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, tc := range []struct {
		path        string
		contentType string
		needle      string
	}{
		{path: "/ui/", contentType: "text/html; charset=utf-8", needle: "owrtd jobs"},
		{path: "/ui/styles.css", contentType: "text/css; charset=utf-8", needle: ".layout"},
		{path: "/ui/app.js", contentType: "application/javascript; charset=utf-8", needle: "/v1/jobs?limit=50"},
	} {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		rec := httptest.NewRecorder()
		srv.handleDashboard(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: want 200, got %d", tc.path, rec.Code)
		}
		if got := rec.Header().Get("Content-Type"); got != tc.contentType {
			t.Fatalf("%s: want content-type %q, got %q", tc.path, tc.contentType, got)
		}
		if !strings.Contains(rec.Body.String(), tc.needle) {
			t.Fatalf("%s: response missing %q", tc.path, tc.needle)
		}
	}
}

func TestDashboardRejectsPostAndMissingAsset(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/ui/", nil)
	rec := httptest.NewRecorder()
	srv.handleDashboard(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /ui/: want 405, got %d", rec.Code)
	}

	req = httptest.NewRequest(http.MethodGet, "/ui/missing.js", nil)
	rec = httptest.NewRecorder()
	srv.handleDashboard(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("missing asset: want 404, got %d", rec.Code)
	}
}

func TestJobsListReturnsRecentSuccessNewestFirst(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_aaaaaaaaaaaa", map[string]any{
		"job_id":      "job_aaaaaaaaaaaa",
		"state":       "SUCCEEDED",
		"success":     true,
		"dry_run":     false,
		"run_dir":     filepath.Join(dir, "job_aaaaaaaaaaaa"),
		"started_at":  "2026-05-08T01:00:00+00:00",
		"finished_at": "2026-05-08T01:05:00+00:00",
	}, "")
	seedJob(t, dir, "job_bbbbbbbbbbbb", map[string]any{
		"job_id":      "job_bbbbbbbbbbbb",
		"state":       "SUCCEEDED",
		"success":     true,
		"dry_run":     false,
		"run_dir":     filepath.Join(dir, "job_bbbbbbbbbbbb"),
		"started_at":  "2026-05-08T02:00:00+00:00",
		"finished_at": "2026-05-08T02:05:00+00:00",
	}, "")

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs?limit=10", nil)
	rec := httptest.NewRecorder()
	srv.handleJobs(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var entries []jobsListEntry
	if err := json.NewDecoder(rec.Body).Decode(&entries); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(entries) != 2 {
		t.Fatalf("want 2 entries, got %d", len(entries))
	}
	// Newest started_at first.
	if entries[0].JobID != "job_bbbbbbbbbbbb" {
		t.Fatalf("want bbbb first, got %s", entries[0].JobID)
	}
	if entries[1].JobID != "job_aaaaaaaaaaaa" {
		t.Fatalf("want aaaa second, got %s", entries[1].JobID)
	}
}

func TestJobsListFallsBackToReportMtimeWhenStartedAtMissing(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_oldmtime", map[string]any{
		"job_id":  "job_oldmtime",
		"state":   "DRY_RUN",
		"success": true,
		"dry_run": true,
		"run_dir": filepath.Join(dir, "job_oldmtime"),
	}, "")
	seedJob(t, dir, "job_newmtime", map[string]any{
		"job_id":  "job_newmtime",
		"state":   "DRY_RUN",
		"success": true,
		"dry_run": true,
		"run_dir": filepath.Join(dir, "job_newmtime"),
	}, "")
	oldTime := time.Date(2026, 5, 14, 1, 0, 0, 0, time.UTC)
	newTime := time.Date(2026, 5, 14, 2, 0, 0, 0, time.UTC)
	if err := os.Chtimes(filepath.Join(dir, "job_oldmtime", "report.json"), oldTime, oldTime); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(dir, "job_newmtime", "report.json"), newTime, newTime); err != nil {
		t.Fatal(err)
	}

	entries, err := srv.listJobs(10)
	if err != nil {
		t.Fatalf("list jobs: %v", err)
	}
	if len(entries) != 2 {
		t.Fatalf("want 2 entries, got %d", len(entries))
	}
	if entries[0].JobID != "job_newmtime" {
		t.Fatalf("want newest mtime first, got %s", entries[0].JobID)
	}
}

func TestJobsListLimitParamRejectsBogus(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, bad := range []string{"-1", "0", "9999", "notanumber"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/jobs?limit="+bad, nil)
		rec := httptest.NewRecorder()
		srv.handleJobs(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("limit=%q: want 400, got %d", bad, rec.Code)
		}
	}
}

func TestJobsPostSubmitsRunner(t *testing.T) {
	srv, _ := newTestServer(t)
	recordPath := filepath.Join(t.TempDir(), "runner-record.txt")
	runner := filepath.Join(t.TempDir(), "fake-runner")
	if err := os.WriteFile(runner, []byte("#!/bin/sh\n"+
		"printf 'job=%s\\n' \"$OWRT_MONITOR_JOB_ID\" > \"$OWRT_FAKE_RUNNER_RECORD\"\n"+
		"for arg in \"$@\"; do printf 'arg=%s\\n' \"$arg\" >> \"$OWRT_FAKE_RUNNER_RECORD\"; done\n"+
		"printf 'stdout from runner\\n'\n"+
		"printf 'stderr from runner\\n' >&2\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("OWRT_FAKE_RUNNER_RECORD", recordPath)
	srv.runnerBin = runner

	body := `{"command":"run","config":"configs/example.yaml","profile":"ap","dry_run":true,"allow_flash":true}`
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.handleJobs(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp jobSubmitResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !strings.HasPrefix(resp.JobID, "job_") {
		t.Fatalf("want generated job id, got %q", resp.JobID)
	}
	if resp.PID <= 0 {
		t.Fatalf("want pid > 0, got %d", resp.PID)
	}
	if resp.RunDir != filepath.Join(srv.artifactsDir, resp.JobID) {
		t.Fatalf("run_dir mismatch: %q", resp.RunDir)
	}
	if !waitForFileContains(recordPath, "job="+resp.JobID, 5*time.Second) {
		t.Fatalf("runner record missing job id; contents=%q", readFileBestEffort(recordPath))
	}
	if !waitForFileContains(recordPath, "arg=--allow-flash", 5*time.Second) {
		t.Fatalf("runner record missing final arg; contents=%q", readFileBestEffort(recordPath))
	}
	record := readFileBestEffort(recordPath)
	for _, want := range []string{
		"arg=run",
		"arg=--config",
		"arg=configs/example.yaml",
		"arg=--profile",
		"arg=ap",
		"arg=--dry-run",
		"arg=--allow-flash",
	} {
		if !strings.Contains(record, want) {
			t.Fatalf("runner record missing %q:\n%s", want, record)
		}
	}
	if !waitForFileContains(resp.RunnerLog, "stdout from runner", 5*time.Second) ||
		!strings.Contains(readFileBestEffort(resp.RunnerLog), "stderr from runner") {
		t.Fatalf("runner log missing output; contents=%q", readFileBestEffort(resp.RunnerLog))
	}
	if !waitForFileContains(resp.RunnerOutput, `"stream":"stdout"`, 5*time.Second) ||
		!strings.Contains(readFileBestEffort(resp.RunnerOutput), `"stream":"stderr"`) {
		t.Fatalf("runner output missing structured streams; contents=%q",
			readFileBestEffort(resp.RunnerOutput))
	}
	if !strings.Contains(readFileBestEffort(resp.RunnerOutput), `"line":"stdout from runner"`) ||
		!strings.Contains(readFileBestEffort(resp.RunnerOutput), `"line":"stderr from runner"`) {
		t.Fatalf("runner output missing line payloads; contents=%q",
			readFileBestEffort(resp.RunnerOutput))
	}
	status, ok := waitForRunnerStatus(filepath.Join(resp.RunDir, "runner.json"), "exited", 5*time.Second)
	if !ok {
		t.Fatalf("runner status did not reach exited; contents=%q",
			readFileBestEffort(filepath.Join(resp.RunDir, "runner.json")))
	}
	if status.JobID != resp.JobID {
		t.Fatalf("runner status job id mismatch: got %q want %q", status.JobID, resp.JobID)
	}
	if status.PID != resp.PID {
		t.Fatalf("runner status pid mismatch: got %d want %d", status.PID, resp.PID)
	}
	if status.ExitCode == nil || *status.ExitCode != 0 {
		t.Fatalf("want exit_code 0, got %v", status.ExitCode)
	}
	if len(status.Command) == 0 || status.Command[0] != runner {
		t.Fatalf("runner status command missing fake runner: %#v", status.Command)
	}
	if status.RunnerOutput != resp.RunnerOutput {
		t.Fatalf("runner output path mismatch: got %q want %q", status.RunnerOutput, resp.RunnerOutput)
	}
}

func TestJobsPostHeartbeatsWhileRunnerActive(t *testing.T) {
	srv, _ := newTestServer(t)
	srv.runnerHeartbeatEvery = 10 * time.Millisecond
	runner := filepath.Join(t.TempDir(), "slow-runner")
	if err := os.WriteFile(runner, []byte("#!/bin/sh\nsleep 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	srv.runnerBin = runner

	body := `{"command":"build","config":"configs/example.yaml"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.handleJobs(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp jobSubmitResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	statusPath := filepath.Join(resp.RunDir, "runner.json")
	first, ok := waitForRunnerStatus(statusPath, "running", 5*time.Second)
	if !ok {
		t.Fatalf("runner status did not reach running; contents=%q", readFileBestEffort(statusPath))
	}
	if _, ok := waitForRunnerUpdatedAfter(statusPath, first.UpdatedAt, 5*time.Second); !ok {
		t.Fatalf("runner heartbeat did not refresh updated_at; contents=%q", readFileBestEffort(statusPath))
	}
	if _, ok := waitForRunnerStatus(statusPath, "exited", 5*time.Second); !ok {
		t.Fatalf("runner status did not reach exited; contents=%q", readFileBestEffort(statusPath))
	}
}

func TestJobsPostTruncatesRunnerOutputWhenLimitExceeded(t *testing.T) {
	srv, _ := newTestServer(t)
	srv.runnerOutputMaxBytes = 1
	runner := filepath.Join(t.TempDir(), "noisy-runner")
	if err := os.WriteFile(runner, []byte("#!/bin/sh\n"+
		"printf 'this line should trigger truncation\\n'\n"+
		"printf 'stderr should be drained too\\n' >&2\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	srv.runnerBin = runner

	body := `{"command":"build","config":"configs/example.yaml"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.handleJobs(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp jobSubmitResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	statusPath := filepath.Join(resp.RunDir, "runner.json")
	status, ok := waitForRunnerStatus(statusPath, "exited", 5*time.Second)
	if !ok {
		t.Fatalf("runner status did not reach exited; contents=%q", readFileBestEffort(statusPath))
	}
	if !status.OutputTruncated {
		t.Fatalf("want output_truncated=true, got %#v", status)
	}
	output := readFileBestEffort(resp.RunnerOutput)
	if !strings.Contains(output, "runner output truncated after 1 bytes") {
		t.Fatalf("runner output missing truncation marker: %q", output)
	}
	if strings.Contains(output, "stderr should be drained too") {
		t.Fatalf("runner output should discard lines after truncation: %q", output)
	}
}

func TestJobsPostRotatesRunnerOutputWhenLimitExceeded(t *testing.T) {
	srv, _ := newTestServer(t)
	srv.runnerOutputRotateBytes = 1
	srv.runnerOutputRotateFiles = 2
	runner := filepath.Join(t.TempDir(), "rotating-runner")
	if err := os.WriteFile(runner, []byte("#!/bin/sh\n"+
		"printf 'first runner line\\n'\n"+
		"printf 'second runner line\\n'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	srv.runnerBin = runner

	body := `{"command":"build","config":"configs/example.yaml"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.handleJobs(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp jobSubmitResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	statusPath := filepath.Join(resp.RunDir, "runner.json")
	status, ok := waitForRunnerStatus(statusPath, "exited", 5*time.Second)
	if !ok {
		t.Fatalf("runner status did not reach exited; contents=%q", readFileBestEffort(statusPath))
	}
	if !status.OutputRotated {
		t.Fatalf("want output_rotated=true, got %#v", status)
	}
	if _, err := os.Stat(resp.RunnerLog + ".1"); err != nil {
		t.Fatalf("runner log did not rotate: %v", err)
	}
	if _, err := os.Stat(resp.RunnerOutput + ".1"); err != nil {
		t.Fatalf("runner output did not rotate: %v", err)
	}
	rotated := readFileBestEffort(resp.RunnerOutput + ".1")
	current := readFileBestEffort(resp.RunnerOutput)
	if !strings.Contains(rotated, "first runner line") {
		t.Fatalf("rotated output missing first line: %q", rotated)
	}
	if !strings.Contains(current, "second runner line") {
		t.Fatalf("current output missing second line: %q", current)
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/jobs/"+resp.JobID+"/runner-output", nil)
	rec = httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	output := rec.Body.String()
	firstAt := strings.Index(output, "first runner line")
	secondAt := strings.Index(output, "second runner line")
	if firstAt < 0 || secondAt < 0 || firstAt > secondAt {
		t.Fatalf("runner-output did not stream rotated files in order: %q", output)
	}
}

func TestJobsPostRejectsInvalidRequest(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, body := range []string{
		`{"command":"unknown","config":"configs/example.yaml"}`,
		`{"command":"flash","config":"configs/example.yaml","artifact":"firmware.bin"}`,
		`{"command":"build","config":"configs/example.yaml","allow_flash":true}`,
	} {
		req := httptest.NewRequest(http.MethodPost, "/v1/jobs", strings.NewReader(body))
		rec := httptest.NewRecorder()
		srv.handleJobs(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body=%s: want 400, got %d body=%s", body, rec.Code, rec.Body.String())
		}
	}
}

func TestJobByIDReturnsReport(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_test123", map[string]any{
		"job_id":  "job_test123",
		"state":   "SUCCEEDED",
		"success": true,
	}, "")

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_test123", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["job_id"] != "job_test123" {
		t.Fatalf("want job_id job_test123, got %v", body["job_id"])
	}
}

func TestJobByIDReturns404ForMissing(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_does_not_exist", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestDeleteJobRemovesRunDirectory(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_delete123"
	seedJob(t, dir, jobID, map[string]any{
		"job_id":  jobID,
		"state":   "SUCCEEDED",
		"success": true,
	}, "")
	if err := os.WriteFile(filepath.Join(dir, jobID, "runner.log"), []byte("done\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodDelete, "/v1/jobs/"+jobID, nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body jobDeleteResponse
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.JobID != jobID || !body.Removed {
		t.Fatalf("unexpected response: %#v", body)
	}
	if _, err := os.Stat(filepath.Join(dir, jobID)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("job dir still exists or unexpected error: %v", err)
	}
}

func TestDeleteJobReturns404ForMissing(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodDelete, "/v1/jobs/job_missing_delete", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestDeleteJobRejectsActiveRunner(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_active_delete"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	srv.processAlive = func(pid int) bool { return pid == 12345 }
	if err := writeRunnerStatus(filepath.Join(dir, jobID, "runner.json"), runnerStatus{
		JobID:  jobID,
		Status: "running",
		PID:    12345,
	}); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodDelete, "/v1/jobs/"+jobID, nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("want 409, got %d body=%s", rec.Code, rec.Body.String())
	}
	if _, err := os.Stat(filepath.Join(dir, jobID)); err != nil {
		t.Fatalf("job dir should remain: %v", err)
	}
}

func TestDeleteJobRejectsOwnedLock(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_locked_delete"
	now := time.Now().UTC().Format(time.RFC3339Nano)
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), locksSnapshot{
		GeneratedAt: now,
		DutLocks: []dutLock{{
			DutName:     "dut-a",
			OwnerJobID:  jobID,
			CreatedAt:   now,
			HeartbeatAt: now,
		}},
	})

	req := httptest.NewRequest(http.MethodDelete, "/v1/jobs/"+jobID, nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("want 409, got %d body=%s", rec.Code, rec.Body.String())
	}
	if _, err := os.Stat(filepath.Join(dir, jobID)); err != nil {
		t.Fatalf("job dir should remain: %v", err)
	}
}

func TestJobByIDRejectsPathTraversal(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, bad := range []string{"..", "../etc", "x/../y", "abs/path"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+bad, nil)
		rec := httptest.NewRecorder()
		srv.handleJobByID(rec, req)
		if rec.Code != http.StatusBadRequest && rec.Code != http.StatusNotFound {
			t.Fatalf("path %q: want 400 or 404, got %d", bad, rec.Code)
		}
	}
}

func TestJobEventsStreamsRawJSONL(t *testing.T) {
	srv, dir := newTestServer(t)
	events := `{"event":"a","ts":"t1"}` + "\n" + `{"event":"b","ts":"t2"}` + "\n"
	seedJob(t, dir, "job_events1", map[string]any{"job_id": "job_events1"}, events)

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_events1/events", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/x-ndjson" {
		t.Fatalf("want application/x-ndjson, got %q", got)
	}
	if rec.Body.String() != events {
		t.Fatalf("body mismatch:\nwant %q\ngot  %q", events, rec.Body.String())
	}
}

func TestCancelWritesMarkerAndReturns202(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_cancel_me1"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	runDir := filepath.Join(dir, jobID)
	status := runnerStatus{
		JobID:        jobID,
		Status:       "running",
		PID:          1234,
		Command:      []string{"owrt-monitor", "run"},
		RunDir:       runDir,
		RunnerLog:    filepath.Join(runDir, "runner.log"),
		RunnerOutput: filepath.Join(runDir, "runner.output.jsonl"),
		StartedAt:    "2026-05-14T00:00:00Z",
		UpdatedAt:    "2026-05-14T00:00:00Z",
	}
	if err := writeRunnerStatus(filepath.Join(runDir, "runner.json"), status); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/jobs/"+jobID+"/cancel", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
	// Marker file must exist on disk with the same shape Python writes.
	marker := filepath.Join(runDir, "cancel.flag")
	contents, err := os.ReadFile(marker)
	if err != nil {
		t.Fatalf("marker file: %v", err)
	}
	if string(contents) != "requested\n" {
		t.Fatalf("marker contents: want %q, got %q", "requested\n", string(contents))
	}

	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["status"] != "cancellation requested" {
		t.Fatalf("want cancellation requested, got %v", body["status"])
	}
	updated, ok := readRunnerStatusBestEffort(filepath.Join(runDir, "runner.json"))
	if !ok {
		t.Fatalf("runner status missing after cancel")
	}
	if updated.Status != "cancel_requested" {
		t.Fatalf("want runner status cancel_requested, got %q", updated.Status)
	}
	if updated.CancelRequestedAt == "" {
		t.Fatalf("want cancel_requested_at to be populated")
	}
}

func TestCancelReturns404WhenJobDirMissing(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs/job_unknown/cancel", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestCancelGetReturns405(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_cancel_get1", map[string]any{"job_id": "job_cancel_get1"}, "")

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_cancel_get1/cancel", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("want 405, got %d", rec.Code)
	}
}

func TestFilesServesBuildLog(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_files_log123"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	if err := os.WriteFile(filepath.Join(dir, jobID, "build.log"),
		[]byte("hello build log\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/files/build.log", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "hello build log") {
		t.Fatalf("body missing payload: %q", rec.Body.String())
	}
}

func TestFilesServesNestedFirmware(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_files_fw1234"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	fwDir := filepath.Join(dir, jobID, "firmware")
	if err := os.MkdirAll(fwDir, 0o755); err != nil {
		t.Fatal(err)
	}
	payload := []byte("FAKE_FIRMWARE_BYTES")
	if err := os.WriteFile(filepath.Join(fwDir, "openwrt.bin"), payload, 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/files/firmware/openwrt.bin", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if !bytes.Equal(rec.Body.Bytes(), payload) {
		t.Fatalf("body mismatch")
	}
}

func TestFilesPathTraversalRejected(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_files_trav1"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	// Place a sibling file outside the job dir; path traversal must NOT reach it.
	sibling := filepath.Join(dir, "secret.txt")
	if err := os.WriteFile(sibling, []byte("SECRET"), 0o644); err != nil {
		t.Fatal(err)
	}

	for _, suffix := range []string{
		"../secret.txt",
		"..%2Fsecret.txt",
		"foo/../../secret.txt",
	} {
		req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/files/"+suffix, nil)
		rec := httptest.NewRecorder()
		srv.handleJobByID(rec, req)
		// http.FileServer either redirects or 404s; what matters is the
		// secret content never appears in the response body.
		if bytes.Contains(rec.Body.Bytes(), []byte("SECRET")) {
			t.Fatalf("path traversal leaked secret for %q (status=%d body=%q)",
				suffix, rec.Code, rec.Body.String())
		}
	}
}

func TestFilesReturns404WhenJobMissing(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_no_exist/files/anything", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestFilesPostReturns405(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_files_post1"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")

	req := httptest.NewRequest(http.MethodPost, "/v1/jobs/"+jobID+"/files/build.log", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("want 405, got %d", rec.Code)
	}
}

func TestEventsPostReturns405(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_events_post", map[string]any{"job_id": "job_events_post"}, "x\n")

	req := httptest.NewRequest(http.MethodPost, "/v1/jobs/job_events_post/events", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("want 405, got %d", rec.Code)
	}
}

func TestJobEventsReturns404WhenAbsent(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_no_events", map[string]any{"job_id": "job_no_events"}, "")

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_no_events/events", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestJobAnalysisReturnsJSON(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_analysis"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	payload := []byte(`{"kind":"advisory_analysis","job":{"job_id":"job_analysis"}}`)
	if err := os.WriteFile(filepath.Join(dir, jobID, "analysis.json"), payload, 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/analysis", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["kind"] != "advisory_analysis" {
		t.Fatalf("unexpected analysis body: %#v", body)
	}
}

func TestJobAnalysisReturns404WhenAbsent(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_no_analysis", map[string]any{"job_id": "job_no_analysis"}, "")

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_no_analysis/analysis", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestJobRunnerReturnsStatus(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_runner_status"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	exitCode := 0
	status := runnerStatus{
		JobID:        jobID,
		Status:       "exited",
		PID:          1234,
		Command:      []string{"owrt-monitor", "run", "--config", "config.yaml"},
		RunDir:       runDir,
		RunnerLog:    filepath.Join(runDir, "runner.log"),
		RunnerOutput: filepath.Join(runDir, "runner.output.jsonl"),
		StartedAt:    "2026-05-14T00:00:00Z",
		UpdatedAt:    "2026-05-14T00:00:01Z",
		FinishedAt:   "2026-05-14T00:00:01Z",
		ExitCode:     &exitCode,
	}
	if err := writeRunnerStatus(filepath.Join(runDir, "runner.json"), status); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body runnerStatus
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Status != "exited" || body.ExitCode == nil || *body.ExitCode != 0 {
		t.Fatalf("unexpected runner status: %#v", body)
	}
}

func TestJobRunnerMarksDeadActivePidAsOrphaned(t *testing.T) {
	srv, dir := newTestServer(t)
	srv.processAlive = func(pid int) bool { return false }
	jobID := "job_runner_orphan"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	status := runnerStatus{
		JobID:        jobID,
		Status:       "running",
		PID:          424242,
		Command:      []string{"owrt-monitor", "build"},
		RunDir:       runDir,
		RunnerLog:    filepath.Join(runDir, "runner.log"),
		RunnerOutput: filepath.Join(runDir, "runner.output.jsonl"),
		StartedAt:    "2026-05-14T00:00:00Z",
		UpdatedAt:    "2026-05-14T00:00:01Z",
	}
	statusPath := filepath.Join(runDir, "runner.json")
	if err := writeRunnerStatus(statusPath, status); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body runnerStatus
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Status != "orphaned" {
		t.Fatalf("want orphaned, got %#v", body)
	}
	if body.OrphanedAt == "" || body.FinishedAt == "" {
		t.Fatalf("want orphaned_at and finished_at populated: %#v", body)
	}
	if !strings.Contains(body.Error, "424242") {
		t.Fatalf("want error to mention pid, got %q", body.Error)
	}
	persisted, ok := readRunnerStatusBestEffort(statusPath)
	if !ok {
		t.Fatalf("runner status missing after reconciliation")
	}
	if persisted.Status != "orphaned" {
		t.Fatalf("want persisted orphaned status, got %#v", persisted)
	}
}

func TestJobRunnerKeepsActiveStatusWhenPidAlive(t *testing.T) {
	srv, dir := newTestServer(t)
	srv.processAlive = func(pid int) bool { return true }
	jobID := "job_runner_alive"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	status := runnerStatus{
		JobID:        jobID,
		Status:       "running",
		PID:          1234,
		Command:      []string{"owrt-monitor", "build"},
		RunDir:       runDir,
		RunnerLog:    filepath.Join(runDir, "runner.log"),
		RunnerOutput: filepath.Join(runDir, "runner.output.jsonl"),
		StartedAt:    "2026-05-14T00:00:00Z",
		UpdatedAt:    "2026-05-14T00:00:01Z",
	}
	if err := writeRunnerStatus(filepath.Join(runDir, "runner.json"), status); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body runnerStatus
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Status != "running" {
		t.Fatalf("want running, got %#v", body)
	}
	if body.OrphanedAt != "" {
		t.Fatalf("did not expect orphaned_at: %#v", body)
	}
}

func TestJobRunnerReturns404WhenAbsent(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_no_runner", map[string]any{"job_id": "job_no_runner"}, "")

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_no_runner/runner", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestJobRunnerOutputStreamsRawJSONL(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_runner_output"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	payload := `{"ts":"t1","job_id":"job_runner_output","stream":"stdout","line":"hello"}` + "\n" +
		`{"ts":"t2","job_id":"job_runner_output","stream":"stderr","line":"warn"}` + "\n"
	if err := os.WriteFile(filepath.Join(runDir, "runner.output.jsonl"), []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner-output", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/x-ndjson" {
		t.Fatalf("want application/x-ndjson, got %q", got)
	}
	if rec.Body.String() != payload {
		t.Fatalf("body mismatch:\nwant %q\ngot  %q", payload, rec.Body.String())
	}
}

func TestJobRunnerOutputTail(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_runner_tail"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	payload := `{"line":"one"}` + "\n" + `{"line":"two"}` + "\n" + `{"line":"three"}` + "\n"
	if err := os.WriteFile(filepath.Join(runDir, "runner.output.jsonl"), []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner-output?tail=2", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	want := `{"line":"two"}` + "\n" + `{"line":"three"}` + "\n"
	if rec.Body.String() != want {
		t.Fatalf("tail mismatch:\nwant %q\ngot  %q", want, rec.Body.String())
	}
}

func TestJobRunnerOutputTailIncludesRotatedFiles(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_runner_tail_rotated"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	rotated := `{"line":"one"}` + "\n" + `{"line":"two"}` + "\n"
	current := `{"line":"three"}` + "\n"
	if err := os.WriteFile(filepath.Join(runDir, "runner.output.jsonl.1"), []byte(rotated), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(runDir, "runner.output.jsonl"), []byte(current), 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner-output?tail=2", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	want := `{"line":"two"}` + "\n" + `{"line":"three"}` + "\n"
	if rec.Body.String() != want {
		t.Fatalf("tail mismatch:\nwant %q\ngot  %q", want, rec.Body.String())
	}
}

func TestJobRunnerOutputFollowExitsWhenRunnerFinished(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_runner_follow_done"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	payload := `{"line":"done"}` + "\n"
	if err := os.WriteFile(filepath.Join(runDir, "runner.output.jsonl"), []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}
	exitCode := 0
	status := runnerStatus{
		JobID:        jobID,
		Status:       "exited",
		Command:      []string{"owrt-monitor", "build"},
		RunDir:       runDir,
		RunnerLog:    filepath.Join(runDir, "runner.log"),
		RunnerOutput: filepath.Join(runDir, "runner.output.jsonl"),
		StartedAt:    "2026-05-14T00:00:00Z",
		UpdatedAt:    "2026-05-14T00:00:01Z",
		FinishedAt:   "2026-05-14T00:00:01Z",
		ExitCode:     &exitCode,
	}
	if err := writeRunnerStatus(filepath.Join(runDir, "runner.json"), status); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner-output?follow=true", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if rec.Body.String() != payload {
		t.Fatalf("follow body mismatch: want %q got %q", payload, rec.Body.String())
	}
}

func TestJobRunnerOutputRejectsBadTail(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, tail := range []string{"0", "-1", "notint", "10001"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_any/runner-output?tail="+tail, nil)
		rec := httptest.NewRecorder()
		srv.handleJobByID(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("tail=%q: want 400, got %d", tail, rec.Code)
		}
	}
}

func TestJobRunnerOutputReturns404WhenAbsent(t *testing.T) {
	srv, dir := newTestServer(t)
	seedJob(t, dir, "job_no_runner_output", map[string]any{"job_id": "job_no_runner_output"}, "")

	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_no_runner_output/runner-output", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestLocksReturnsEmptyWhenSnapshotMissing(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/locks", nil)
	rec := httptest.NewRecorder()
	srv.handleLocks(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if duts, _ := body["dut_locks"].([]any); len(duts) != 0 {
		t.Fatalf("want empty dut_locks, got %v", duts)
	}
}

func TestLocksReadsSnapshot(t *testing.T) {
	srv, dir := newTestServer(t)
	snapshot := []byte(`{
  "generated_at": "2026-05-08T03:14:15+00:00",
  "dut_locks": [
    {"dut_name": "dut-01", "owner_job_id": "job_abc", "created_at": "x", "heartbeat_at": "y"}
  ],
  "builder_locks": []
}`)
	if err := os.WriteFile(filepath.Join(dir, "locks.json"), snapshot, 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/locks", nil)
	rec := httptest.NewRecorder()
	srv.handleLocks(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	duts := body["dut_locks"].([]any)
	if len(duts) != 1 {
		t.Fatalf("want 1 dut_lock, got %d", len(duts))
	}
	first := duts[0].(map[string]any)
	if first["dut_name"] != "dut-01" {
		t.Fatalf("want dut-01, got %v", first["dut_name"])
	}
	if first["owner_job_id"] != "job_abc" {
		t.Fatalf("want job_abc, got %v", first["owner_job_id"])
	}
}

func TestLocksRejectsBadSnapshotJSON(t *testing.T) {
	srv, dir := newTestServer(t)
	if err := os.WriteFile(filepath.Join(dir, "locks.json"),
		[]byte("not json {"), 0o644); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/v1/locks", nil)
	rec := httptest.NewRecorder()
	srv.handleLocks(rec, req)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", rec.Code)
	}
}

func TestLocksPostReturns405(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/locks", nil)
	rec := httptest.NewRecorder()
	srv.handleLocks(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("want 405, got %d", rec.Code)
	}
}

func TestLockAcquireDUTWritesSnapshot(t *testing.T) {
	srv, dir := newTestServer(t)
	req := httptest.NewRequest(
		http.MethodPost,
		"/v1/locks/dut/dut-a/acquire",
		strings.NewReader(`{"owner_job_id":"job1","lock_timeout_sec":60}`),
	)
	rec := httptest.NewRecorder()
	srv.handleLockByID(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["acquired"] != true || body["type"] != "dut" {
		t.Fatalf("unexpected acquire response: %#v", body)
	}
	snapshot := readLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"))
	if len(snapshot.DutLocks) != 1 {
		t.Fatalf("want 1 dut lock, got %#v", snapshot.DutLocks)
	}
	if snapshot.DutLocks[0].DutName != "dut-a" ||
		snapshot.DutLocks[0].OwnerJobID != "job1" {
		t.Fatalf("unexpected dut lock: %#v", snapshot.DutLocks[0])
	}
}

func TestLockAcquireBuilderConflict(t *testing.T) {
	srv, _ := newTestServer(t)
	first := httptest.NewRequest(
		http.MethodPost,
		"/v1/locks/container/bld-a/acquire",
		strings.NewReader(`{"owner_job_id":"job1"}`),
	)
	rec := httptest.NewRecorder()
	srv.handleLockByID(rec, first)
	if rec.Code != http.StatusCreated {
		t.Fatalf("first acquire: want 201, got %d body=%s", rec.Code, rec.Body.String())
	}

	second := httptest.NewRequest(
		http.MethodPost,
		"/v1/locks/builder/bld-a/acquire",
		strings.NewReader(`{"owner_job_id":"job2"}`),
	)
	rec = httptest.NewRecorder()
	srv.handleLockByID(rec, second)
	if rec.Code != http.StatusConflict {
		t.Fatalf("second acquire: want 409, got %d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["owner_job_id"] != "job1" || body["acquired"] != false {
		t.Fatalf("unexpected conflict response: %#v", body)
	}
}

func TestLockAcquireBreaksStaleBuilderLock(t *testing.T) {
	srv, dir := newTestServer(t)
	stale := time.Now().Add(-2 * time.Hour).UTC().Format(time.RFC3339Nano)
	snapshot := locksSnapshot{
		GeneratedAt: "2026-05-14T00:00:00Z",
		DutLocks:    []dutLock{},
		BuilderLocks: []builderLock{{
			BuilderName: "bld-a",
			OwnerJobID:  "old-job",
			CreatedAt:   stale,
			HeartbeatAt: stale,
		}},
	}
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), snapshot)

	req := httptest.NewRequest(
		http.MethodPost,
		"/v1/locks/builder/bld-a/acquire",
		strings.NewReader(`{"owner_job_id":"new-job","lock_timeout_sec":60}`),
	)
	rec := httptest.NewRecorder()
	srv.handleLockByID(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d body=%s", rec.Code, rec.Body.String())
	}
	updated := readLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"))
	if len(updated.BuilderLocks) != 1 || updated.BuilderLocks[0].OwnerJobID != "new-job" {
		t.Fatalf("stale lock was not replaced: %#v", updated.BuilderLocks)
	}
}

func TestLockHeartbeatRefreshesOwnedLock(t *testing.T) {
	srv, dir := newTestServer(t)
	old := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339Nano)
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), locksSnapshot{
		GeneratedAt: "2026-05-14T00:00:00Z",
		DutLocks: []dutLock{{
			DutName:     "dut-a",
			OwnerJobID:  "job1",
			CreatedAt:   old,
			HeartbeatAt: old,
		}},
		BuilderLocks: []builderLock{},
	})

	req := httptest.NewRequest(
		http.MethodPost,
		"/v1/locks/dut/dut-a/heartbeat",
		strings.NewReader(`{"owner_job_id":"job1"}`),
	)
	rec := httptest.NewRecorder()
	srv.handleLockByID(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	updated := readLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"))
	if updated.DutLocks[0].HeartbeatAt == old {
		t.Fatalf("heartbeat did not change: %#v", updated.DutLocks[0])
	}
}

func TestLockReleaseRequiresOwner(t *testing.T) {
	srv, dir := newTestServer(t)
	now := time.Now().UTC().Format(time.RFC3339Nano)
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), locksSnapshot{
		GeneratedAt: "2026-05-14T00:00:00Z",
		DutLocks: []dutLock{{
			DutName:     "dut-a",
			OwnerJobID:  "job1",
			CreatedAt:   now,
			HeartbeatAt: now,
		}},
		BuilderLocks: []builderLock{},
	})

	req := httptest.NewRequest(
		http.MethodPost,
		"/v1/locks/dut/dut-a/release",
		strings.NewReader(`{"owner_job_id":"job2"}`),
	)
	rec := httptest.NewRecorder()
	srv.handleLockByID(rec, req)
	if rec.Code != http.StatusConflict {
		t.Fatalf("wrong-owner release: want 409, got %d body=%s", rec.Code, rec.Body.String())
	}
	if got := readLocksSnapshotForTest(t, filepath.Join(dir, "locks.json")); len(got.DutLocks) != 1 {
		t.Fatalf("wrong-owner release removed lock: %#v", got.DutLocks)
	}

	req = httptest.NewRequest(
		http.MethodPost,
		"/v1/locks/dut/dut-a/release",
		strings.NewReader(`{"owner_job_id":"job1"}`),
	)
	rec = httptest.NewRecorder()
	srv.handleLockByID(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("owner release: want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	updated := readLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"))
	if len(updated.DutLocks) != 0 {
		t.Fatalf("owner release did not remove lock: %#v", updated.DutLocks)
	}
}

func TestLockAcquireSerialAndArtifactLocks(t *testing.T) {
	srv, dir := newTestServer(t)
	for _, target := range []string{
		"/v1/locks/serial/tty-usb0/acquire",
		"/v1/locks/artifact/export-root/acquire",
	} {
		req := httptest.NewRequest(
			http.MethodPost,
			target,
			strings.NewReader(`{"owner_job_id":"job1"}`),
		)
		rec := httptest.NewRecorder()
		srv.handleLockByID(rec, req)
		if rec.Code != http.StatusCreated {
			t.Fatalf("%s: want 201, got %d body=%s", target, rec.Code, rec.Body.String())
		}
	}
	snapshot := readLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"))
	if len(snapshot.SerialLocks) != 1 || snapshot.SerialLocks[0].Name != "tty-usb0" {
		t.Fatalf("serial lock missing: %#v", snapshot.SerialLocks)
	}
	if len(snapshot.ArtifactLocks) != 1 || snapshot.ArtifactLocks[0].Name != "export-root" {
		t.Fatalf("artifact lock missing: %#v", snapshot.ArtifactLocks)
	}
}

func TestIsSafeJobID(t *testing.T) {
	cases := map[string]bool{
		"job_abc123":       true,
		"job_aaaaaaaaaaaa": true,
		"with-hyphen":      true,
		"":                 false,
		"..":               false,
		"x/y":              false,
		"x y":              false,
		"中":                false, // non-ASCII
	}
	for input, want := range cases {
		if got := isSafeJobID(input); got != want {
			t.Errorf("isSafeJobID(%q) = %v, want %v", input, got, want)
		}
	}
}

func TestWriteJSONSetsContentTypeAndStatus(t *testing.T) {
	rec := httptest.NewRecorder()
	type payload struct {
		N int `json:"n"`
	}
	writeJSON(rec, http.StatusTeapot, payload{N: 42})
	if rec.Code != http.StatusTeapot {
		t.Fatalf("want 418, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf(`want "application/json", got %q`, got)
	}
	var body payload
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.N != 42 {
		t.Fatalf("want N=42, got %d", body.N)
	}
}

func TestListJobsHandlesMissingArtifactsDir(t *testing.T) {
	srv := &server{artifactsDir: filepath.Join(t.TempDir(), "does_not_exist")}
	entries, err := srv.listJobs(50)
	if err != nil {
		t.Fatalf("want nil err, got %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("want 0 entries, got %d", len(entries))
	}
}

func TestListJobsSkipsCorruptReports(t *testing.T) {
	srv, dir := newTestServer(t)
	// Valid job
	seedJob(t, dir, "job_valid_one1", map[string]any{
		"job_id": "job_valid_one1", "started_at": "2026-05-08T00:00:00+00:00",
	}, "")
	// Corrupt report
	bad := filepath.Join(dir, "job_corrupt0001")
	if err := os.MkdirAll(bad, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bad, "report.json"), []byte("not json {"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Non-job dir
	if err := os.MkdirAll(filepath.Join(dir, "not_a_job_dir"), 0o755); err != nil {
		t.Fatal(err)
	}

	entries, err := srv.listJobs(50)
	if err != nil {
		t.Fatalf("want nil err, got %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("want 1 entry, got %d (%v)", len(entries), entries)
	}
	if entries[0].JobID != "job_valid_one1" {
		t.Fatalf("want job_valid_one1, got %s", entries[0].JobID)
	}
}

func waitForFileContains(path, needle string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if strings.Contains(readFileBestEffort(path), needle) {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return strings.Contains(readFileBestEffort(path), needle)
}

func waitForRunnerStatus(path, want string, timeout time.Duration) (runnerStatus, bool) {
	deadline := time.Now().Add(timeout)
	for {
		data, err := os.ReadFile(path)
		if err == nil {
			var status runnerStatus
			if json.Unmarshal(data, &status) == nil && status.Status == want {
				return status, true
			}
		}
		if !time.Now().Before(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	return runnerStatus{}, false
}

func waitForRunnerUpdatedAfter(path, previous string, timeout time.Duration) (runnerStatus, bool) {
	deadline := time.Now().Add(timeout)
	for {
		data, err := os.ReadFile(path)
		if err == nil {
			var status runnerStatus
			if json.Unmarshal(data, &status) == nil &&
				status.Status == "running" &&
				status.UpdatedAt != "" &&
				status.UpdatedAt != previous {
				return status, true
			}
		}
		if !time.Now().Before(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	return runnerStatus{}, false
}

func writeLocksSnapshotForTest(t *testing.T, path string, snapshot locksSnapshot) {
	t.Helper()
	data, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatalf("marshal locks snapshot: %v", err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatalf("write locks snapshot: %v", err)
	}
}

func readLocksSnapshotForTest(t *testing.T, path string) locksSnapshot {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read locks snapshot: %v", err)
	}
	var snapshot locksSnapshot
	if err := json.Unmarshal(data, &snapshot); err != nil {
		t.Fatalf("decode locks snapshot: %v", err)
	}
	return snapshot
}

func readFileBestEffort(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(data)
}
