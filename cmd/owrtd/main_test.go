package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// newTestServer wires up a `server` rooted at a temp dir so the read-only
// endpoints have something to chew on without touching the real lab.
func newTestServer(t *testing.T) (*server, string) {
	t.Helper()
	dir := t.TempDir()
	return &server{artifactsDir: dir}, dir
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

func TestJobsListPostReturns501(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs", strings.NewReader("{}"))
	rec := httptest.NewRecorder()
	srv.handleJobs(rec, req)
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d", rec.Code)
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
	seedJob(t, dir, "job_cancel_me1", map[string]any{"job_id": "job_cancel_me1"}, "")

	req := httptest.NewRequest(http.MethodPost, "/v1/jobs/job_cancel_me1/cancel", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
	// Marker file must exist on disk with the same shape Python writes.
	marker := filepath.Join(dir, "job_cancel_me1", "cancel.flag")
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

func TestIsSafeJobID(t *testing.T) {
	cases := map[string]bool{
		"job_abc123":          true,
		"job_aaaaaaaaaaaa":    true,
		"with-hyphen":         true,
		"":                    false,
		"..":                  false,
		"x/y":                 false,
		"x y":                 false,
		"中":              false, // non-ASCII
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
