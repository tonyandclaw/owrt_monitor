package main

import (
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
