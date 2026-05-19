package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestJobSubmitRequestCLIArgsVariants(t *testing.T) {
	tests := []struct {
		name string
		req  jobSubmitRequest
		want []string
	}{
		{
			name: "build",
			req:  jobSubmitRequest{Command: " build ", Config: "config.yaml", Profile: " ap ", DryRun: true},
			want: []string{"build", "--config", "config.yaml", "--profile", "ap", "--dry-run"},
		},
		{
			name: "run",
			req:  jobSubmitRequest{Command: "run", Config: "config.yaml", AllowFlash: true},
			want: []string{"run", "--config", "config.yaml", "--allow-flash"},
		},
		{
			name: "flash",
			req: jobSubmitRequest{
				Command:    "flash",
				Config:     "config.yaml",
				Artifact:   "firmware.bin",
				DryRun:     true,
				AllowFlash: true,
			},
			want: []string{
				"flash", "--artifact", "firmware.bin", "--config", "config.yaml", "--dry-run", "--allow-flash",
			},
		},
		{
			name: "test",
			req:  jobSubmitRequest{Command: "test", Config: "config.yaml"},
			want: []string{"test", "--config", "config.yaml"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := tc.req.cliArgs()
			if err != nil {
				t.Fatalf("cliArgs: %v", err)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("args mismatch:\ngot  %#v\nwant %#v", got, tc.want)
			}
		})
	}
}

func TestJobSubmitRequestCLIArgsErrors(t *testing.T) {
	tests := []struct {
		name string
		req  jobSubmitRequest
		want string
	}{
		{name: "missing command", req: jobSubmitRequest{Config: "config.yaml"}, want: "command is required"},
		{name: "missing config", req: jobSubmitRequest{Command: "build"}, want: "config is required"},
		{name: "build artifact", req: jobSubmitRequest{Command: "build", Config: "config.yaml", Artifact: "x"}, want: "build does not accept artifact"},
		{name: "run artifact", req: jobSubmitRequest{Command: "run", Config: "config.yaml", Artifact: "x"}, want: "run does not accept artifact"},
		{name: "flash missing artifact", req: jobSubmitRequest{Command: "flash", Config: "config.yaml", AllowFlash: true}, want: "artifact is required"},
		{name: "flash missing allow", req: jobSubmitRequest{Command: "flash", Config: "config.yaml", Artifact: "x"}, want: "requires allow_flash"},
		{name: "test allow flash", req: jobSubmitRequest{Command: "test", Config: "config.yaml", AllowFlash: true}, want: "test does not accept allow_flash"},
		{name: "test artifact", req: jobSubmitRequest{Command: "test", Config: "config.yaml", Artifact: "x"}, want: "test does not accept artifact"},
		{name: "unsupported", req: jobSubmitRequest{Command: "resume", Config: "config.yaml"}, want: "unsupported command"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tc.req.cliArgs()
			if err == nil {
				t.Fatalf("expected error")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q missing %q", err, tc.want)
			}
		})
	}
}

func TestHandleJobsRejectsMethodBadJSONUnknownFieldAndStartFailure(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, tc := range []struct {
		name   string
		method string
		body   string
		want   int
	}{
		{name: "bad method", method: http.MethodPut, body: "", want: http.StatusMethodNotAllowed},
		{name: "bad json", method: http.MethodPost, body: "{", want: http.StatusBadRequest},
		{name: "unknown field", method: http.MethodPost, body: `{"command":"build","config":"config.yaml","extra":true}`, want: http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.method, "/v1/jobs", strings.NewReader(tc.body))
			rec := httptest.NewRecorder()
			srv.handleJobs(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("want %d, got %d body=%s", tc.want, rec.Code, rec.Body.String())
			}
		})
	}

	srv.runnerBin = filepath.Join(t.TempDir(), "missing-runner")
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs", strings.NewReader(`{"command":"build","config":"config.yaml"}`))
	rec := httptest.NewRecorder()
	srv.handleJobs(rec, req)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestHandleJobByIDRejectsInvalidShapesAndMethods(t *testing.T) {
	srv, _ := newTestServer(t)
	tests := []struct {
		name   string
		method string
		path   string
		want   int
	}{
		{name: "missing id", method: http.MethodGet, path: "/v1/jobs/", want: http.StatusNotFound},
		{name: "bad id", method: http.MethodGet, path: "/v1/jobs/bad%20id", want: http.StatusBadRequest},
		{name: "bare bad method", method: http.MethodPost, path: "/v1/jobs/job_a", want: http.StatusMethodNotAllowed},
		{name: "analysis nested", method: http.MethodGet, path: "/v1/jobs/job_a/analysis/extra", want: http.StatusNotFound},
		{name: "analysis bad method", method: http.MethodPost, path: "/v1/jobs/job_a/analysis", want: http.StatusMethodNotAllowed},
		{name: "events nested", method: http.MethodGet, path: "/v1/jobs/job_a/events/extra", want: http.StatusNotFound},
		{name: "runner nested", method: http.MethodGet, path: "/v1/jobs/job_a/runner/extra", want: http.StatusNotFound},
		{name: "runner bad method", method: http.MethodPost, path: "/v1/jobs/job_a/runner", want: http.StatusMethodNotAllowed},
		{name: "runner-output nested", method: http.MethodGet, path: "/v1/jobs/job_a/runner-output/extra", want: http.StatusNotFound},
		{name: "runner-output bad method", method: http.MethodPost, path: "/v1/jobs/job_a/runner-output", want: http.StatusMethodNotAllowed},
		{name: "cancel nested", method: http.MethodPost, path: "/v1/jobs/job_a/cancel/extra", want: http.StatusNotFound},
		{name: "unknown subresource", method: http.MethodGet, path: "/v1/jobs/job_a/nope", want: http.StatusNotFound},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.method, tc.path, nil)
			rec := httptest.NewRecorder()
			srv.handleJobByID(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("want %d, got %d body=%s", tc.want, rec.Code, rec.Body.String())
			}
		})
	}
}

func TestCorruptJSONSubresourcesReturn500(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_corrupt_subresources"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name string
		file string
		path string
	}{
		{name: "report", file: "report.json", path: "/v1/jobs/" + jobID},
		{name: "analysis", file: "analysis.json", path: "/v1/jobs/" + jobID + "/analysis"},
		{name: "runner", file: "runner.json", path: "/v1/jobs/" + jobID + "/runner"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := os.WriteFile(filepath.Join(runDir, tc.file), []byte("not json {"), 0o644); err != nil {
				t.Fatal(err)
			}
			req := httptest.NewRequest(http.MethodGet, tc.path, nil)
			rec := httptest.NewRecorder()
			srv.handleJobByID(rec, req)
			if rec.Code != http.StatusInternalServerError {
				t.Fatalf("want 500, got %d body=%s", rec.Code, rec.Body.String())
			}
		})
	}
}

func TestDeleteJobHandlesFileAndLockOwnerKinds(t *testing.T) {
	srv, dir := newTestServer(t)
	if err := os.WriteFile(filepath.Join(dir, "job_file"), []byte("not a dir"), 0o644); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodDelete, "/v1/jobs/job_file", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404 for file job, got %d", rec.Code)
	}

	if _, err := srv.jobRunDir("bad/id"); err == nil {
		t.Fatalf("expected unsafe job id error")
	}

	snapshot := locksSnapshot{
		BuilderLocks:  []builderLock{{BuilderName: "bld", OwnerJobID: "job_x"}},
		SerialLocks:   []namedLock{{Name: "tty", OwnerJobID: "job_y"}},
		ArtifactLocks: []namedLock{{Name: "export", OwnerJobID: "job_z"}},
	}
	for _, tc := range []struct {
		job  string
		want string
	}{
		{job: "job_x", want: "builder lock"},
		{job: "job_y", want: "serial lock"},
		{job: "job_z", want: "artifact lock"},
	} {
		reason, ok := lockOwnedByJob(snapshot, tc.job)
		if !ok || !strings.Contains(reason, tc.want) {
			t.Fatalf("lockOwnedByJob(%s) = %q, %v; want %q", tc.job, reason, ok, tc.want)
		}
	}
}

func TestDeleteJobReportsLockReadError(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_delete_lock_error"
	seedJob(t, dir, jobID, map[string]any{"job_id": jobID}, "")
	if err := os.WriteFile(filepath.Join(dir, "locks.json"), []byte("{"), 0o644); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodDelete, "/v1/jobs/"+jobID, nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestLockByIDRejectsBadRequests(t *testing.T) {
	srv, _ := newTestServer(t)
	tests := []struct {
		name   string
		method string
		path   string
		body   string
		want   int
	}{
		{name: "bad method", method: http.MethodGet, path: "/v1/locks/dut/dut-a/acquire", body: `{}`, want: http.StatusMethodNotAllowed},
		{name: "bad shape", method: http.MethodPost, path: "/v1/locks/dut/dut-a", body: `{}`, want: http.StatusNotFound},
		{name: "bad kind", method: http.MethodPost, path: "/v1/locks/nope/dut-a/acquire", body: `{}`, want: http.StatusBadRequest},
		{name: "bad name", method: http.MethodPost, path: "/v1/locks/dut/./acquire", body: `{}`, want: http.StatusBadRequest},
		{name: "bad json", method: http.MethodPost, path: "/v1/locks/dut/dut-a/acquire", body: `{`, want: http.StatusBadRequest},
		{name: "unknown field", method: http.MethodPost, path: "/v1/locks/dut/dut-a/acquire", body: `{"owner_job_id":"job1","extra":true}`, want: http.StatusBadRequest},
		{name: "bad owner", method: http.MethodPost, path: "/v1/locks/dut/dut-a/acquire", body: `{"owner_job_id":"bad/id"}`, want: http.StatusBadRequest},
		{name: "bad action", method: http.MethodPost, path: "/v1/locks/dut/dut-a/nope", body: `{"owner_job_id":"job1"}`, want: http.StatusNotFound},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
			rec := httptest.NewRecorder()
			srv.handleLockByID(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("want %d, got %d body=%s", tc.want, rec.Code, rec.Body.String())
			}
		})
	}
}

func TestLockHeartbeatAndReleaseAllKinds(t *testing.T) {
	srv, dir := newTestServer(t)
	now := time.Now().UTC().Format(time.RFC3339Nano)
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), locksSnapshot{
		GeneratedAt: now,
		BuilderLocks: []builderLock{{
			BuilderName: "bld-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
		SerialLocks: []namedLock{{
			Name: "tty-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
		ArtifactLocks: []namedLock{{
			Name: "artifact-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
	})

	for _, tc := range []struct {
		name string
		path string
		body string
		want int
	}{
		{name: "heartbeat missing", path: "/v1/locks/dut/missing/heartbeat", body: `{"owner_job_id":"job1"}`, want: http.StatusNotFound},
		{name: "heartbeat wrong owner", path: "/v1/locks/builder/bld-a/heartbeat", body: `{"owner_job_id":"job2"}`, want: http.StatusConflict},
		{name: "heartbeat builder", path: "/v1/locks/builder/bld-a/heartbeat", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "heartbeat serial", path: "/v1/locks/serial/tty-a/heartbeat", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "heartbeat artifact", path: "/v1/locks/artifact/artifact-a/heartbeat", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "release missing", path: "/v1/locks/builder/missing/release", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "release wrong owner", path: "/v1/locks/serial/tty-a/release", body: `{"owner_job_id":"job2"}`, want: http.StatusConflict},
		{name: "release serial", path: "/v1/locks/serial/tty-a/release", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "release artifact", path: "/v1/locks/artifact/artifact-a/release", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "release builder", path: "/v1/locks/builder/bld-a/release", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, tc.path, strings.NewReader(tc.body))
			rec := httptest.NewRecorder()
			srv.handleLockByID(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("want %d, got %d body=%s", tc.want, rec.Code, rec.Body.String())
			}
		})
	}
}

func TestLockHeartbeatAndReleaseMissingAndWrongOwnersByKind(t *testing.T) {
	srv, dir := newTestServer(t)
	now := time.Now().UTC().Format(time.RFC3339Nano)
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), locksSnapshot{
		GeneratedAt: now,
		DutLocks: []dutLock{{
			DutName: "dut-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
		BuilderLocks: []builderLock{{
			BuilderName: "bld-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
		SerialLocks: []namedLock{{
			Name: "tty-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
		ArtifactLocks: []namedLock{{
			Name: "artifact-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
	})

	for _, tc := range []struct {
		name string
		path string
		body string
		want int
	}{
		{name: "heartbeat dut wrong owner", path: "/v1/locks/dut/dut-a/heartbeat", body: `{"owner_job_id":"job2"}`, want: http.StatusConflict},
		{name: "heartbeat builder missing", path: "/v1/locks/builder/missing/heartbeat", body: `{"owner_job_id":"job1"}`, want: http.StatusNotFound},
		{name: "heartbeat serial missing", path: "/v1/locks/serial/missing/heartbeat", body: `{"owner_job_id":"job1"}`, want: http.StatusNotFound},
		{name: "heartbeat serial wrong owner", path: "/v1/locks/serial/tty-a/heartbeat", body: `{"owner_job_id":"job2"}`, want: http.StatusConflict},
		{name: "heartbeat artifact missing", path: "/v1/locks/artifact/missing/heartbeat", body: `{"owner_job_id":"job1"}`, want: http.StatusNotFound},
		{name: "heartbeat artifact wrong owner", path: "/v1/locks/artifact/artifact-a/heartbeat", body: `{"owner_job_id":"job2"}`, want: http.StatusConflict},
		{name: "release dut missing", path: "/v1/locks/dut/missing/release", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "release builder wrong owner", path: "/v1/locks/builder/bld-a/release", body: `{"owner_job_id":"job2"}`, want: http.StatusConflict},
		{name: "release serial missing", path: "/v1/locks/serial/missing/release", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "release artifact missing", path: "/v1/locks/artifact/missing/release", body: `{"owner_job_id":"job1"}`, want: http.StatusOK},
		{name: "release artifact wrong owner", path: "/v1/locks/artifact/artifact-a/release", body: `{"owner_job_id":"job2"}`, want: http.StatusConflict},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, tc.path, strings.NewReader(tc.body))
			rec := httptest.NewRecorder()
			srv.handleLockByID(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("want %d, got %d body=%s", tc.want, rec.Code, rec.Body.String())
			}
		})
	}
}

func TestLockAcquireBreaksStaleDutSerialAndArtifactLocks(t *testing.T) {
	srv, dir := newTestServer(t)
	stale := time.Now().Add(-2 * time.Hour).UTC().Format(time.RFC3339Nano)
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), locksSnapshot{
		GeneratedAt: stale,
		DutLocks: []dutLock{{
			DutName: "dut-a", OwnerJobID: "old", CreatedAt: stale, HeartbeatAt: stale,
		}},
		SerialLocks: []namedLock{{
			Name: "tty-a", OwnerJobID: "old", CreatedAt: stale, HeartbeatAt: stale,
		}},
		ArtifactLocks: []namedLock{{
			Name: "artifact-a", OwnerJobID: "old", CreatedAt: stale, HeartbeatAt: stale,
		}},
	})
	for _, target := range []string{
		"/v1/locks/dut/dut-a/acquire",
		"/v1/locks/serial/tty-a/acquire",
		"/v1/locks/artifact/artifact-a/acquire",
	} {
		req := httptest.NewRequest(
			http.MethodPost,
			target,
			strings.NewReader(`{"owner_job_id":"new","lock_timeout_sec":60}`),
		)
		rec := httptest.NewRecorder()
		srv.handleLockByID(rec, req)
		if rec.Code != http.StatusCreated {
			t.Fatalf("%s: want 201, got %d body=%s", target, rec.Code, rec.Body.String())
		}
	}
	snapshot := readLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"))
	if snapshot.DutLocks[0].OwnerJobID != "new" ||
		snapshot.SerialLocks[0].OwnerJobID != "new" ||
		snapshot.ArtifactLocks[0].OwnerJobID != "new" {
		t.Fatalf("stale locks were not replaced: %#v", snapshot)
	}
}

func TestLockHelpers(t *testing.T) {
	for _, raw := range []string{
		time.Now().UTC().Format(time.RFC3339Nano),
		"2026-05-19T01:02:03.123456789",
		"2026-05-19T01:02:03",
	} {
		if _, err := parseLockTimestamp(raw); err != nil {
			t.Fatalf("parseLockTimestamp(%q): %v", raw, err)
		}
	}
	if !lockHeartbeatIsStale("not a timestamp", 60) {
		t.Fatalf("invalid timestamp should be stale")
	}
	for input, want := range map[string]bool{
		"dut-a":                  true,
		"":                       false,
		".":                      false,
		"..":                     false,
		"has/slash":              false,
		`has\backslash`:          false,
		"has\x7fcontrol":         false,
		strings.Repeat("x", 129): false,
	} {
		if got := isSafeLockName(input); got != want {
			t.Fatalf("isSafeLockName(%q)=%v, want %v", input, got, want)
		}
	}

	snapshot := locksSnapshot{
		DutLocks:      []dutLock{{DutName: "z"}, {DutName: "a"}},
		BuilderLocks:  []builderLock{{BuilderName: "z"}, {BuilderName: "a"}},
		SerialLocks:   []namedLock{{Name: "z"}, {Name: "a"}},
		ArtifactLocks: []namedLock{{Name: "z"}, {Name: "a"}},
	}
	sortLocks(&snapshot)
	if snapshot.DutLocks[0].DutName != "a" ||
		snapshot.BuilderLocks[0].BuilderName != "a" ||
		snapshot.SerialLocks[0].Name != "a" ||
		snapshot.ArtifactLocks[0].Name != "a" {
		t.Fatalf("sortLocks did not sort: %#v", snapshot)
	}
}

func TestLockResourceUnsupportedKindsAndSnapshotErrors(t *testing.T) {
	srv, dir := newTestServer(t)
	if _, _, err := srv.acquireResourceLock("unknown", "name", "job1", 0); err == nil {
		t.Fatalf("expected acquire unsupported kind error")
	}
	if _, _, err := srv.heartbeatResourceLock("unknown", "name", "job1"); err == nil {
		t.Fatalf("expected heartbeat unsupported kind error")
	}
	if _, _, err := srv.releaseResourceLock("unknown", "name", "job1"); err == nil {
		t.Fatalf("expected release unsupported kind error")
	}

	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dir, []byte("not a directory"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := srv.writeLocksSnapshotUnlocked(emptyLocksSnapshot()); err == nil {
		t.Fatalf("expected writeLocksSnapshotUnlocked error")
	}

	other, otherDir := newTestServer(t)
	if err := os.Mkdir(filepath.Join(otherDir, "locks.json"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := other.readLocksSnapshot(); err == nil {
		t.Fatalf("expected readLocksSnapshot error")
	}
}

func TestJobsPostUsesWorkingDirAndRecordsNonzeroExit(t *testing.T) {
	srv, _ := newTestServer(t)
	workDir := t.TempDir()
	recordPath := filepath.Join(t.TempDir(), "pwd.txt")
	runner := filepath.Join(t.TempDir(), "failing-runner")
	if err := os.WriteFile(runner, []byte("#!/bin/sh\npwd > \"$OWRT_PWD_RECORD\"\nexit 7\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("OWRT_PWD_RECORD", recordPath)
	srv.runnerBin = runner

	body := `{"command":"build","config":"config.yaml","working_dir":` + quoteJSON(workDir) + `}`
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
	status, ok := waitForRunnerStatus(filepath.Join(resp.RunDir, "runner.json"), "exited", 5*time.Second)
	if !ok {
		t.Fatalf("runner did not exit; status=%s", readFileBestEffort(filepath.Join(resp.RunDir, "runner.json")))
	}
	if status.ExitCode == nil || *status.ExitCode != 7 {
		t.Fatalf("want exit code 7, got %#v", status.ExitCode)
	}
	if status.Error == "" {
		t.Fatalf("nonzero runner exit should record an error")
	}
	gotWorkDir, err := filepath.EvalSymlinks(strings.TrimSpace(readFileBestEffort(recordPath)))
	if err != nil {
		t.Fatal(err)
	}
	wantWorkDir, err := filepath.EvalSymlinks(workDir)
	if err != nil {
		t.Fatal(err)
	}
	if gotWorkDir != wantWorkDir {
		t.Fatalf("runner working dir mismatch: got %q want %q", gotWorkDir, wantWorkDir)
	}
}

func TestRunnerOutputHelperFunctions(t *testing.T) {
	dir := t.TempDir()
	current := filepath.Join(dir, "runner.output.jsonl")
	rotated := current + ".1"
	if err := os.WriteFile(current, []byte("abc\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(rotated, []byte("old-data\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	var out bytes.Buffer
	size, err := copyFileFromOffset(current, 2, &out)
	if err != nil {
		t.Fatalf("copyFileFromOffset: %v", err)
	}
	if size != 4 || out.String() != "c\n" {
		t.Fatalf("copy from offset mismatch size=%d out=%q", size, out.String())
	}

	out.Reset()
	if _, err := copyFileFromOffset(current, 99, &out); err != nil {
		t.Fatalf("copyFileFromOffset reset: %v", err)
	}
	if out.String() != "abc\n" {
		t.Fatalf("offset past EOF should copy from start, got %q", out.String())
	}

	out.Reset()
	size, err = copyFilesFromStart([]string{rotated, current}, &out)
	if err != nil {
		t.Fatalf("copyFilesFromStart: %v", err)
	}
	if size != 4 || out.String() != "old-data\nabc\n" {
		t.Fatalf("copyFilesFromStart mismatch size=%d out=%q", size, out.String())
	}

	out.Reset()
	size, err = copyFollowRunnerOutput(current, 1, &out)
	if err != nil {
		t.Fatalf("copyFollowRunnerOutput current: %v", err)
	}
	if size != 4 || out.String() != "bc\n" {
		t.Fatalf("follow current mismatch size=%d out=%q", size, out.String())
	}

	out.Reset()
	size, err = copyFollowRunnerOutput(current, 5, &out)
	if err != nil {
		t.Fatalf("copyFollowRunnerOutput rotated: %v", err)
	}
	if size != 4 || !strings.Contains(out.String(), "ata\nabc\n") {
		t.Fatalf("follow rotated mismatch size=%d out=%q", size, out.String())
	}

	if _, err := copyFileFromOffset(filepath.Join(dir, "missing"), 0, io.Discard); err == nil {
		t.Fatalf("expected missing file error")
	}
	if _, err := copyFilesFromStart([]string{filepath.Join(dir, "missing")}, io.Discard); err == nil {
		t.Fatalf("expected copyFilesFromStart missing error")
	}
	if _, err := copyFollowRunnerOutput(filepath.Join(dir, "missing"), 0, io.Discard); err == nil {
		t.Fatalf("expected copyFollowRunnerOutput missing error")
	}
}

func TestRunnerOutputWriterDirectPaths(t *testing.T) {
	dir := t.TempDir()
	statusPath := filepath.Join(dir, "runner.json")
	if err := writeRunnerStatus(statusPath, runnerStatus{JobID: "job_writer", Status: "running"}); err != nil {
		t.Fatal(err)
	}

	humanPath := filepath.Join(dir, "runner.log")
	structuredPath := filepath.Join(dir, "runner.output.jsonl")
	human, err := os.OpenFile(humanPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	structured, err := os.OpenFile(structuredPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	writer := &runnerOutputWriter{
		jobID:          "job_writer",
		statusPath:     statusPath,
		humanPath:      humanPath,
		structuredPath: structuredPath,
		human:          human,
		structured:     structured,
		maxBytes:       1024,
	}
	if err := writer.writeLine("stdout", "hello"); err != nil {
		t.Fatalf("writeLine: %v", err)
	}
	if err := writer.close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if !strings.Contains(readFileBestEffort(humanPath), "hello") ||
		!strings.Contains(readFileBestEffort(structuredPath), `"line":"hello"`) {
		t.Fatalf("writer did not write expected output")
	}

	truncHuman, err := os.OpenFile(filepath.Join(dir, "trunc.log"), os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	truncStructured, err := os.OpenFile(filepath.Join(dir, "trunc.output.jsonl"), os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	truncWriter := &runnerOutputWriter{
		jobID:          "job_writer",
		statusPath:     statusPath,
		humanPath:      filepath.Join(dir, "trunc.log"),
		structuredPath: filepath.Join(dir, "trunc.output.jsonl"),
		human:          truncHuman,
		structured:     truncStructured,
		maxBytes:       1,
	}
	if err := truncWriter.writeLine("stdout", "this will truncate"); err != nil {
		t.Fatalf("truncate writeLine: %v", err)
	}
	if err := truncWriter.writeLine("stdout", "discarded"); err != nil {
		t.Fatalf("discarded writeLine: %v", err)
	}
	if !truncWriter.isTruncated() {
		t.Fatalf("writer should be truncated")
	}
	if err := truncWriter.close(); err != nil {
		t.Fatalf("truncate close: %v", err)
	}

	rotateHumanPath := filepath.Join(dir, "rotate.log")
	rotateStructuredPath := filepath.Join(dir, "rotate.output.jsonl")
	rotateHuman, err := os.OpenFile(rotateHumanPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	rotateStructured, err := os.OpenFile(rotateStructuredPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	rotateWriter := &runnerOutputWriter{
		jobID:          "job_writer",
		statusPath:     statusPath,
		humanPath:      rotateHumanPath,
		structuredPath: rotateStructuredPath,
		human:          rotateHuman,
		structured:     rotateStructured,
		rotateBytes:    1,
		rotateFiles:    1,
	}
	if err := rotateWriter.writeLine("stdout", "first"); err != nil {
		t.Fatalf("rotate first writeLine: %v", err)
	}
	if err := rotateWriter.writeLine("stdout", "second"); err != nil {
		t.Fatalf("rotate second writeLine: %v", err)
	}
	if !rotateWriter.isRotated() {
		t.Fatalf("writer should be rotated")
	}
	if err := rotateWriter.close(); err != nil {
		t.Fatalf("rotate close: %v", err)
	}
}

func TestFollowRunnerOutputRequiresFlusher(t *testing.T) {
	srv, dir := newTestServer(t)
	outputPath := filepath.Join(dir, "runner.output.jsonl")
	if err := os.WriteFile(outputPath, []byte(`{"line":"x"}`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	writer := &nonFlushingResponseWriter{header: http.Header{}}
	req := httptest.NewRequest(http.MethodGet, "/runner-output?follow=true", nil)
	srv.followRunnerOutput(writer, req, "job_a", outputPath, []string{outputPath}, 0)
	if writer.status != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d body=%s", writer.status, writer.body.String())
	}
}

func TestRunnerErrorWritersAndStatusHelpers(t *testing.T) {
	rec := httptest.NewRecorder()
	logOrWriteFileError(rec, os.ErrNotExist, "runner.output.jsonl")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("not-exist logOrWriteFileError: want 404, got %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	logOrWriteFileError(rec, errors.New("boom"), "runner.output.jsonl")
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "boom") {
		t.Fatalf("generic logOrWriteFileError mismatch: code=%d body=%s", rec.Code, rec.Body.String())
	}

	rec = httptest.NewRecorder()
	writeRunnerOutputFileError(rec, errors.New("boom"))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("generic writeRunnerOutputFileError: want 500, got %d", rec.Code)
	}

	if got, err := parseBoolQuery("yes"); err != nil || !got {
		t.Fatalf("parseBoolQuery yes = %v, %v", got, err)
	}
	if got, err := parseBoolQuery("0"); err != nil || got {
		t.Fatalf("parseBoolQuery 0 = %v, %v", got, err)
	}
	if _, err := parseBoolQuery("maybe"); err == nil {
		t.Fatalf("expected parseBoolQuery error")
	}
	if processAlive(0) {
		t.Fatalf("pid 0 should not be alive")
	}
	if !processAlive(os.Getpid()) {
		t.Fatalf("current process should be alive")
	}
	srv := &server{processAlive: func(pid int) bool { return pid == 42 }}
	if !srv.runnerProcessAlive(42) || srv.runnerProcessAlive(41) {
		t.Fatalf("custom processAlive hook not used")
	}
	if err := writeRunnerStatus(filepath.Join(t.TempDir(), "missing", "runner.json"), runnerStatus{}); err == nil {
		t.Fatalf("expected writeRunnerStatus error for missing directory")
	}
	rec = httptest.NewRecorder()
	writeJSON(rec, http.StatusOK, map[string]any{"bad": make(chan int)})
	if rec.Code != http.StatusOK {
		t.Fatalf("writeJSON should still set status before encode error")
	}
}

func TestRunnerStatusMarkersMergeAndActiveHelpers(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_runner_helpers"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	statusPath := filepath.Join(runDir, "runner.json")
	if err := writeRunnerStatus(statusPath, runnerStatus{
		JobID:        jobID,
		Status:       "running",
		PID:          123,
		RunDir:       runDir,
		RunnerOutput: filepath.Join(runDir, "runner.output.jsonl"),
		UpdatedAt:    "old",
	}); err != nil {
		t.Fatal(err)
	}
	if err := markRunnerOutputTruncated(statusPath); err != nil {
		t.Fatal(err)
	}
	if err := markRunnerOutputRotated(statusPath); err != nil {
		t.Fatal(err)
	}
	status, ok := readRunnerStatusBestEffort(statusPath)
	if !ok || !status.OutputTruncated || !status.OutputRotated {
		t.Fatalf("marker status mismatch: %#v ok=%v", status, ok)
	}
	status.CancelRequestedAt = "cancel-time"
	if err := writeRunnerStatus(statusPath, status); err != nil {
		t.Fatal(err)
	}
	merged := mergeRunnerCancellation(statusPath, runnerStatus{Status: "running"})
	if merged.Status != "cancel_requested" ||
		merged.CancelRequestedAt != "cancel-time" ||
		!merged.OutputRotated ||
		!merged.OutputTruncated {
		t.Fatalf("mergeRunnerCancellation mismatch: %#v", merged)
	}
	if !srv.runnerIsActive(jobID) {
		t.Fatalf("runner should be active")
	}
	if srv.runnerIsActive("job_missing") {
		t.Fatalf("missing runner should not be active")
	}

	if err := markRunnerOutputTruncated(filepath.Join(dir, "missing.json")); err != nil {
		t.Fatalf("missing mark truncated should be nil: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "bad.json"), []byte("{"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, ok := readRunnerStatusBestEffort(filepath.Join(dir, "bad.json")); ok {
		t.Fatalf("corrupt runner status should not decode")
	}
	if err := markRunnerOutputRotated(filepath.Join(dir, "missing.json")); err != nil {
		t.Fatalf("missing mark rotated should be nil: %v", err)
	}
}

func TestCancelExitedRunnerDoesNotMutateStatus(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_cancel_exited"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	statusPath := filepath.Join(runDir, "runner.json")
	exitCode := 0
	if err := writeRunnerStatus(statusPath, runnerStatus{
		JobID:    jobID,
		Status:   "exited",
		ExitCode: &exitCode,
	}); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs/"+jobID+"/cancel", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
	status, ok := readRunnerStatusBestEffort(statusPath)
	if !ok {
		t.Fatalf("status missing")
	}
	if status.Status != "exited" || status.CancelRequestedAt != "" {
		t.Fatalf("exited status should not be mutated: %#v", status)
	}
}

func TestRunnerOutputFollowTailWithCanceledContext(t *testing.T) {
	srv, dir := newTestServer(t)
	jobID := "job_follow_cancel"
	runDir := filepath.Join(dir, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	outputPath := filepath.Join(runDir, "runner.output.jsonl")
	if err := os.WriteFile(outputPath, []byte(`{"line":"one"}`+"\n"+`{"line":"two"}`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writeRunnerStatus(filepath.Join(runDir, "runner.json"), runnerStatus{
		JobID:        jobID,
		Status:       "running",
		RunDir:       runDir,
		RunnerOutput: outputPath,
	}); err != nil {
		t.Fatal(err)
	}

	// Build a canceled request explicitly; followRunnerOutput should return
	// after writing the requested tail instead of waiting on an active runner.
	rec := httptest.NewRecorder()
	cancelReq := httptest.NewRequest(http.MethodGet, "/v1/jobs/"+jobID+"/runner-output?follow=true&tail=1", nil)
	cancelCtx, cancel := context.WithCancel(cancelReq.Context())
	cancel()
	cancelReq = cancelReq.WithContext(cancelCtx)
	srv.handleJobByID(rec, cancelReq)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), `"line":"one"`) ||
		!strings.Contains(rec.Body.String(), `"line":"two"`) {
		t.Fatalf("follow tail mismatch: %q", rec.Body.String())
	}
}

func TestAdditionalSmallBranches(t *testing.T) {
	jobID, err := newRunnerJobID()
	if err != nil {
		t.Fatalf("newRunnerJobID: %v", err)
	}
	if !strings.HasPrefix(jobID, "job_") || !isSafeJobID(jobID) {
		t.Fatalf("unexpected generated job id: %q", jobID)
	}

	srv, dir := newTestServer(t)
	if runDir, err := srv.jobRunDir("job_ok"); err != nil || filepath.Base(runDir) != "job_ok" {
		t.Fatalf("jobRunDir = %q, %v", runDir, err)
	}
	seedJob(t, dir, "job_files_listing", map[string]any{"job_id": "job_files_listing"}, "")
	req := httptest.NewRequest(http.MethodGet, "/v1/jobs/job_files_listing/files", nil)
	rec := httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("directory listing: want 200, got %d body=%s", rec.Code, rec.Body.String())
	}

	runDir := filepath.Join(dir, "job_bad_follow")
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(runDir, "runner.output.jsonl"), []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	req = httptest.NewRequest(http.MethodGet, "/v1/jobs/job_bad_follow/runner-output?follow=maybe", nil)
	rec = httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("bad follow: want 400, got %d body=%s", rec.Code, rec.Body.String())
	}

	if _, _, err := tailFilesLines([]string{filepath.Join(dir, "missing.jsonl")}, 1); err == nil {
		t.Fatalf("tailFilesLines should fail on missing file")
	}

	statusPath := filepath.Join(runDir, "runner.json")
	if err := writeRunnerStatus(statusPath, runnerStatus{JobID: "job_bad_follow", Status: "start_failed"}); err != nil {
		t.Fatal(err)
	}
	if err := srv.markRunnerCancelRequested("job_bad_follow"); err != nil {
		t.Fatal(err)
	}
	status, ok := readRunnerStatusBestEffort(statusPath)
	if !ok || status.Status != "start_failed" || status.CancelRequestedAt != "" {
		t.Fatalf("start_failed status should not be mutated: %#v ok=%v", status, ok)
	}
	if err := srv.markRunnerCancelRequested("job_missing"); err != nil {
		t.Fatalf("missing runner cancel marker should be nil: %v", err)
	}
	if !(&server{}).runnerProcessAlive(os.Getpid()) {
		t.Fatalf("default runnerProcessAlive should detect current process")
	}

	seedJob(t, dir, "job_cancel_without_runner", map[string]any{"job_id": "job_cancel_without_runner"}, "")
	req = httptest.NewRequest(http.MethodPost, "/v1/jobs/job_cancel_without_runner/cancel", nil)
	rec = httptest.NewRecorder()
	srv.handleJobByID(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("cancel without runner: want 202, got %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestLockAcquireConflictsForAllResourceKinds(t *testing.T) {
	srv, dir := newTestServer(t)
	now := time.Now().UTC().Format(time.RFC3339Nano)
	writeLocksSnapshotForTest(t, filepath.Join(dir, "locks.json"), locksSnapshot{
		GeneratedAt: now,
		DutLocks: []dutLock{{
			DutName: "dut-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
		SerialLocks: []namedLock{{
			Name: "tty-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
		ArtifactLocks: []namedLock{{
			Name: "artifact-a", OwnerJobID: "job1", CreatedAt: now, HeartbeatAt: now,
		}},
	})
	for _, target := range []string{
		"/v1/locks/dut/dut-a/acquire",
		"/v1/locks/serial/tty-a/acquire",
		"/v1/locks/artifact/artifact-a/acquire",
	} {
		req := httptest.NewRequest(http.MethodPost, target, strings.NewReader(`{"owner_job_id":"job2"}`))
		rec := httptest.NewRecorder()
		srv.handleLockByID(rec, req)
		if rec.Code != http.StatusConflict {
			t.Fatalf("%s: want 409, got %d body=%s", target, rec.Code, rec.Body.String())
		}
	}
}

type nonFlushingResponseWriter struct {
	header http.Header
	status int
	body   bytes.Buffer
}

func (w *nonFlushingResponseWriter) Header() http.Header {
	return w.header
}

func (w *nonFlushingResponseWriter) Write(data []byte) (int, error) {
	return w.body.Write(data)
}

func (w *nonFlushingResponseWriter) WriteHeader(status int) {
	w.status = status
}

func quoteJSON(value string) string {
	data, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return string(data)
}
