package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func runCLI(t *testing.T, handler http.HandlerFunc, args ...string) (string, string, int) {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := (&cli{
		stdout: &stdout,
		stderr: &stderr,
		client: server.Client(),
	}).run(append([]string{"--daemon-url", server.URL}, args...))
	return stdout.String(), stderr.String(), code
}

func TestJobsCommandListsJobs(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("method = %s, want GET", r.Method)
		}
		if r.URL.Path != "/v1/jobs" {
			t.Fatalf("path = %s, want /v1/jobs", r.URL.Path)
		}
		if r.URL.Query().Get("limit") != "2" {
			t.Fatalf("limit = %s, want 2", r.URL.Query().Get("limit"))
		}
		writeJSONForTest(t, w, []jobEntry{
			{
				JobID:     "job_a",
				State:     "SUCCEEDED",
				Success:   true,
				RunDir:    "/tmp/job_a",
				StartedAt: "2026-05-19T00:00:00Z",
			},
		})
	}, "jobs", "--limit", "2")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	for _, want := range []string{"JOB ID", "job_a", "SUCCEEDED", "/tmp/job_a"} {
		if !strings.Contains(stdout, want) {
			t.Fatalf("stdout missing %q:\n%s", want, stdout)
		}
	}
}

func TestStatusCommandFetchesRunner(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/jobs/job_a/runner" {
			t.Fatalf("path = %s, want runner endpoint", r.URL.Path)
		}
		writeJSONForTest(t, w, map[string]any{
			"job_id": "job_a",
			"status": "running",
			"pid":    1234,
		})
	}, "status", "job_a")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, `"status": "running"`) {
		t.Fatalf("stdout missing status:\n%s", stdout)
	}
}

func TestHealthCommandFetchesHealthz(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("method = %s, want GET", r.Method)
		}
		if r.URL.Path != "/healthz" {
			t.Fatalf("path = %s, want /healthz", r.URL.Path)
		}
		writeJSONForTest(t, w, map[string]any{"status": "ok"})
	}, "health")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, `"status": "ok"`) {
		t.Fatalf("stdout missing health:\n%s", stdout)
	}
}

func TestBuildCommandSubmitsJob(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(configPath, []byte("project: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("method = %s, want POST", r.Method)
		}
		if r.URL.Path != "/v1/jobs" {
			t.Fatalf("path = %s, want /v1/jobs", r.URL.Path)
		}
		var req jobSubmitRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if req.Command != "build" || req.Config != configPath || req.Profile != "ap" {
			t.Fatalf("unexpected request: %#v", req)
		}
		if !req.DryRun || req.AllowFlash {
			t.Fatalf("unexpected flags: %#v", req)
		}
		writeJSONForTest(t, w, jobSubmitResponse{
			JobID:  "job_submit",
			PID:    123,
			Status: "accepted",
			RunDir: "/tmp/job_submit",
		})
	}, "build", "--config", configPath, "--profile", "ap", "--dry-run")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, `"job_id": "job_submit"`) {
		t.Fatalf("stdout missing submit response:\n%s", stdout)
	}
}

func TestSubmitCommandsBuildRequests(t *testing.T) {
	temp := t.TempDir()
	configPath := filepath.Join(temp, "config.yaml")
	artifactPath := filepath.Join(temp, "firmware.bin")
	workingDir := filepath.Join(temp, "work")
	if err := os.WriteFile(configPath, []byte("project: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(artifactPath, []byte("firmware"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(workingDir, 0o755); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name string
		args []string
		want jobSubmitRequest
	}{
		{
			name: "run",
			args: []string{"run", "--config", configPath, "--profile", "ap", "--allow-flash", "--working-dir", workingDir},
			want: jobSubmitRequest{Command: "run", Config: configPath, Profile: "ap", AllowFlash: true, WorkingDir: workingDir},
		},
		{
			name: "flash dry run",
			args: []string{"flash", "--config", configPath, "--artifact", artifactPath, "--dry-run"},
			want: jobSubmitRequest{Command: "flash", Config: configPath, Artifact: artifactPath, DryRun: true},
		},
		{
			name: "dry-run alias",
			args: []string{"dry-run", "--config", configPath},
			want: jobSubmitRequest{Command: "build", Config: configPath, DryRun: true},
		},
		{
			name: "test",
			args: []string{"test", "--config", configPath, "--profile", "ap"},
			want: jobSubmitRequest{Command: "test", Config: configPath, Profile: "ap"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1/jobs" || r.Method != http.MethodPost {
					t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
				}
				var req jobSubmitRequest
				if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
					t.Fatal(err)
				}
				if req.Command != tc.want.Command ||
					req.Config != tc.want.Config ||
					req.Profile != tc.want.Profile ||
					req.DryRun != tc.want.DryRun ||
					req.AllowFlash != tc.want.AllowFlash ||
					req.Artifact != tc.want.Artifact {
					t.Fatalf("unexpected request:\ngot  %#v\nwant %#v", req, tc.want)
				}
				if tc.want.WorkingDir != "" && req.WorkingDir != tc.want.WorkingDir {
					t.Fatalf("working dir = %q, want %q", req.WorkingDir, tc.want.WorkingDir)
				}
				if req.WorkingDir == "" || !filepath.IsAbs(req.WorkingDir) {
					t.Fatalf("working dir should be absolute, got %q", req.WorkingDir)
				}
				writeJSONForTest(t, w, jobSubmitResponse{JobID: "job_submit", Status: "accepted"})
			}, tc.args...)
			if code != 0 {
				t.Fatalf("code = %d stderr=%q", code, stderr)
			}
			if !strings.Contains(stdout, `"status": "accepted"`) {
				t.Fatalf("stdout missing submit response:\n%s", stdout)
			}
		})
	}
}

func TestFlashCommandRequiresAllowFlashUnlessDryRun(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.yaml")
	artifactPath := filepath.Join(t.TempDir(), "firmware.bin")
	if err := os.WriteFile(configPath, []byte("project: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(artifactPath, []byte("firmware"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, stderr, code := runCLI(t, func(http.ResponseWriter, *http.Request) {
		t.Fatal("server should not be called")
	}, "flash", "--config", configPath, "--artifact", artifactPath)

	if code == 0 {
		t.Fatalf("code = 0, want non-zero")
	}
	if !strings.Contains(stderr, "flash requires --allow-flash") {
		t.Fatalf("stderr missing allow-flash guidance:\n%s", stderr)
	}
}

func TestBuildCommandRejectsAllowFlash(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(configPath, []byte("project: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, stderr, code := runCLI(t, func(http.ResponseWriter, *http.Request) {
		t.Fatal("server should not be called")
	}, "build", "--config", configPath, "--allow-flash")

	if code == 0 {
		t.Fatalf("code = 0, want non-zero")
	}
	if !strings.Contains(stderr, "build does not accept --allow-flash") {
		t.Fatalf("stderr missing allow-flash rejection:\n%s", stderr)
	}
}

func TestSubmitCommandRejectsBadArguments(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.yaml")
	artifactPath := filepath.Join(t.TempDir(), "firmware.bin")
	if err := os.WriteFile(configPath, []byte("project: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(artifactPath, []byte("firmware"), 0o644); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name string
		args []string
		want string
	}{
		{name: "missing config", args: []string{"build"}, want: "--config is required"},
		{name: "positional", args: []string{"build", "--config", configPath, "extra"}, want: "does not accept positional"},
		{name: "artifact on build", args: []string{"build", "--config", configPath, "--artifact", artifactPath}, want: "does not accept --artifact"},
		{name: "test allow flash", args: []string{"test", "--config", configPath, "--allow-flash"}, want: "does not accept --allow-flash"},
		{name: "flash missing artifact", args: []string{"flash", "--config", configPath, "--allow-flash"}, want: "--artifact is required"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, stderr, code := runCLI(t, func(http.ResponseWriter, *http.Request) {
				t.Fatal("server should not be called")
			}, tc.args...)
			if code == 0 {
				t.Fatalf("code = 0, want non-zero")
			}
			if !strings.Contains(stderr, tc.want) {
				t.Fatalf("stderr missing %q:\n%s", tc.want, stderr)
			}
		})
	}
}

func TestLogsCommandFormatsRunnerOutput(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/jobs/job_a/runner-output" {
			t.Fatalf("path = %s, want runner-output endpoint", r.URL.Path)
		}
		if r.URL.Query().Get("tail") != "5" {
			t.Fatalf("tail = %s, want 5", r.URL.Query().Get("tail"))
		}
		w.Header().Set("Content-Type", "application/x-ndjson")
		_, _ = w.Write([]byte(
			`{"ts":"t1","stream":"stdout","line":"hello"}` + "\n" +
				`{"ts":"t2","stream":"stderr","line":"warn"}` + "\n",
		))
	}, "logs", "job_a", "--tail", "5")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, "t1 stdout hello") ||
		!strings.Contains(stdout, "t2 stderr warn") {
		t.Fatalf("unexpected stdout:\n%s", stdout)
	}
}

func TestLogsCommandRawCopiesRunnerOutput(t *testing.T) {
	payload := `{"line":"raw"}` + "\n"
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/jobs/job_a/runner-output" {
			t.Fatalf("path = %s, want runner-output endpoint", r.URL.Path)
		}
		if r.URL.Query().Get("follow") != "true" {
			t.Fatalf("follow = %s, want true", r.URL.Query().Get("follow"))
		}
		_, _ = io.WriteString(w, payload)
	}, "logs", "job_a", "--tail=9", "--follow", "--raw")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if stdout != payload {
		t.Fatalf("stdout = %q, want %q", stdout, payload)
	}
}

func TestLogsCommandHandlesPlainLinesAndValidation(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "plain line\n{\"line\":\"json without metadata\"}\n")
	}, "logs", "job_a")
	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, "plain line") || !strings.Contains(stdout, "json without metadata") {
		t.Fatalf("unexpected stdout:\n%s", stdout)
	}

	for _, args := range [][]string{
		{"logs", "job_a", "--tail", "0"},
		{"logs", "job_a", "--tail", "bad"},
		{"logs", "job_a", "--unknown"},
		{"logs", "job_a", "--daemon-url"},
	} {
		_, stderr, code = runCLI(t, func(http.ResponseWriter, *http.Request) {
			t.Fatal("server should not be called")
		}, args...)
		if code == 0 {
			t.Fatalf("%v: code = 0, want non-zero", args)
		}
		if stderr == "" {
			t.Fatalf("%v: expected stderr", args)
		}
	}
}

func TestWaitCommandPollsUntilExited(t *testing.T) {
	calls := 0
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/jobs/job_a/runner" {
			t.Fatalf("path = %s, want runner endpoint", r.URL.Path)
		}
		calls++
		status := "running"
		var exitCode *int
		if calls > 1 {
			status = "exited"
			zero := 0
			exitCode = &zero
		}
		writeJSONForTest(t, w, runnerStatus{
			JobID:     "job_a",
			Status:    status,
			PID:       123,
			ExitCode:  exitCode,
			UpdatedAt: "now",
		})
	}, "wait", "job_a", "--interval", "1ms", "--timeout", "1s")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if calls != 2 {
		t.Fatalf("calls = %d, want 2", calls)
	}
	if !strings.Contains(stdout, "status=running") ||
		!strings.Contains(stdout, "status=exited") {
		t.Fatalf("stdout missing wait statuses:\n%s", stdout)
	}
}

func TestWaitCommandReportsTerminalFailures(t *testing.T) {
	tests := []struct {
		name   string
		status runnerStatus
		want   string
	}{
		{
			name: "nonzero exit",
			status: runnerStatus{
				JobID: "job_a", Status: "exited", PID: 123, ExitCode: intPtr(7), UpdatedAt: "now",
			},
			want: "runner exited with code 7",
		},
		{
			name:   "start failed",
			status: runnerStatus{JobID: "job_a", Status: "start_failed", Error: "boom", UpdatedAt: "now"},
			want:   "runner start_failed: boom",
		},
		{
			name:   "orphaned",
			status: runnerStatus{JobID: "job_a", Status: "orphaned", UpdatedAt: "now"},
			want:   "runner orphaned",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, stderr, code := runCLI(t, func(w http.ResponseWriter, _ *http.Request) {
				writeJSONForTest(t, w, tc.status)
			}, "wait", "job_a", "--interval=1ms", "--timeout=1s")
			if code == 0 {
				t.Fatalf("code = 0, want non-zero")
			}
			if !strings.Contains(stderr, tc.want) {
				t.Fatalf("stderr missing %q:\n%s", tc.want, stderr)
			}
		})
	}
}

func TestWaitCommandTimesOut(t *testing.T) {
	_, stderr, code := runCLI(t, func(w http.ResponseWriter, _ *http.Request) {
		writeJSONForTest(t, w, runnerStatus{
			JobID: "job_a", Status: "running", PID: 123, UpdatedAt: time.Now().Format(time.RFC3339Nano),
		})
	}, "wait", "job_a", "--interval", "1ms", "--timeout", "2ms")
	if code == 0 {
		t.Fatalf("code = 0, want non-zero")
	}
	if !strings.Contains(stderr, "timeout waiting for job_a") {
		t.Fatalf("stderr missing timeout:\n%s", stderr)
	}
}

func TestWaitCommandRejectsBadFlags(t *testing.T) {
	for _, args := range [][]string{
		{"wait", "job_a", "--interval", "0s"},
		{"wait", "job_a", "--interval", "bad"},
		{"wait", "job_a", "--timeout", "-1s"},
		{"wait", "job_a", "--timeout", "bad"},
		{"wait", "job_a", "--unknown"},
		{"wait", "job_a", "--daemon-url"},
	} {
		_, stderr, code := runCLI(t, func(http.ResponseWriter, *http.Request) {
			t.Fatal("server should not be called")
		}, args...)
		if code == 0 {
			t.Fatalf("%v: code = 0, want non-zero", args)
		}
		if stderr == "" {
			t.Fatalf("%v: expected stderr", args)
		}
	}
}

func TestFileCommandCopiesRunDirFile(t *testing.T) {
	outputPath := filepath.Join(t.TempDir(), "build.log")
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("method = %s, want GET", r.Method)
		}
		if r.URL.Path != "/v1/jobs/job_a/files/build.log" {
			t.Fatalf("path = %s, want files endpoint", r.URL.Path)
		}
		_, _ = io.WriteString(w, "build output\n")
	}, "file", "job_a", "build.log", "--output", outputPath)

	if code != 0 {
		t.Fatalf("code = %d stderr=%q stdout=%q", code, stderr, stdout)
	}
	data, err := os.ReadFile(outputPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "build output\n" {
		t.Fatalf("unexpected file contents: %q", string(data))
	}
}

func TestFileCommandScopesAndEscapesRunDirPath(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("method = %s, want GET", r.Method)
		}
		if r.URL.EscapedPath() != "/v1/jobs/job_a/files/firmware/openwrt%20image.bin" {
			t.Fatalf("escaped path = %s", r.URL.EscapedPath())
		}
		_, _ = io.WriteString(w, "firmware")
	}, "file", "job_a", "firmware/openwrt image.bin")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if stdout != "firmware" {
		t.Fatalf("unexpected stdout: %q", stdout)
	}
}

func TestFileCommandRejectsPathTraversal(t *testing.T) {
	_, stderr, code := runCLI(t, func(http.ResponseWriter, *http.Request) {
		t.Fatal("server should not be called")
	}, "file", "job_a", "../runner.json")

	if code == 0 {
		t.Fatalf("code = 0, want non-zero")
	}
	if !strings.Contains(stderr, "invalid run directory path") {
		t.Fatalf("stderr missing path validation:\n%s", stderr)
	}
}

func TestFileCommandRejectsBadArgs(t *testing.T) {
	for _, args := range [][]string{
		{"file", "job_a"},
		{"file", "job_a", "report.md", "--output"},
		{"file", "job_a", "report.md", "--output", ""},
		{"file", "job_a", "report.md", "--unknown"},
		{"file", "job_a", "report.md", "--daemon-url"},
	} {
		_, stderr, code := runCLI(t, func(http.ResponseWriter, *http.Request) {
			t.Fatal("server should not be called")
		}, args...)
		if code == 0 {
			t.Fatalf("%v: code = 0, want non-zero", args)
		}
		if stderr == "" {
			t.Fatalf("%v: expected stderr", args)
		}
	}
}

func TestCancelCommandPostsCancel(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("method = %s, want POST", r.Method)
		}
		if r.URL.Path != "/v1/jobs/job_a/cancel" {
			t.Fatalf("path = %s, want cancel endpoint", r.URL.Path)
		}
		writeJSONForTest(t, w, map[string]any{
			"job_id": "job_a",
			"status": "cancellation requested",
		})
	}, "cancel", "job_a")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, "cancellation requested") {
		t.Fatalf("stdout missing cancel response:\n%s", stdout)
	}
}

func TestRemoveCommandDeletesJob(t *testing.T) {
	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Fatalf("method = %s, want DELETE", r.Method)
		}
		if r.URL.Path != "/v1/jobs/job_a" {
			t.Fatalf("path = %s, want job endpoint", r.URL.Path)
		}
		writeJSONForTest(t, w, map[string]any{
			"job_id":  "job_a",
			"removed": true,
		})
	}, "remove", "job_a")

	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, `"removed": true`) {
		t.Fatalf("stdout missing remove response:\n%s", stdout)
	}
}

func TestReportAnalysisEventsAndLocksCommands(t *testing.T) {
	tests := []struct {
		command string
		args    []string
		method  string
		path    string
		body    any
		want    string
	}{
		{command: "report", args: []string{"report", "job_a"}, method: http.MethodGet, path: "/v1/jobs/job_a", body: map[string]any{"job_id": "job_a"}, want: `"job_id": "job_a"`},
		{command: "analysis", args: []string{"analysis", "job_a"}, method: http.MethodGet, path: "/v1/jobs/job_a/analysis", body: map[string]any{"kind": "analysis"}, want: `"kind": "analysis"`},
		{command: "locks", args: []string{"locks"}, method: http.MethodGet, path: "/v1/locks", body: map[string]any{"generated_at": "now"}, want: `"generated_at": "now"`},
	}
	for _, tc := range tests {
		t.Run(tc.command, func(t *testing.T) {
			stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method != tc.method || r.URL.Path != tc.path {
					t.Fatalf("request = %s %s, want %s %s", r.Method, r.URL.Path, tc.method, tc.path)
				}
				writeJSONForTest(t, w, tc.body)
			}, tc.args...)
			if code != 0 {
				t.Fatalf("code = %d stderr=%q", code, stderr)
			}
			if !strings.Contains(stdout, tc.want) {
				t.Fatalf("stdout missing %q:\n%s", tc.want, stdout)
			}
		})
	}

	stdout, stderr, code := runCLI(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/jobs/job_a/events" {
			t.Fatalf("request = %s %s, want events", r.Method, r.URL.Path)
		}
		_, _ = io.WriteString(w, "event\n")
	}, "events", "job_a")
	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr)
	}
	if stdout != "event\n" {
		t.Fatalf("stdout = %q", stdout)
	}
}

func TestUsageHelpAndUnknownCommand(t *testing.T) {
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	c := &cli{stdout: &stdout, stderr: &stderr, client: http.DefaultClient}
	if code := c.run(nil); code != 0 {
		t.Fatalf("no args code = %d", code)
	}
	if !strings.Contains(stdout.String(), "Usage:") {
		t.Fatalf("usage missing:\n%s", stdout.String())
	}

	stdout.Reset()
	if code := c.run([]string{"help"}); code != 0 {
		t.Fatalf("help code = %d", code)
	}
	if !strings.Contains(stdout.String(), "OWRTD_URL") {
		t.Fatalf("help missing env section:\n%s", stdout.String())
	}

	stderr.Reset()
	if code := c.run([]string{"nope"}); code == 0 {
		t.Fatalf("unknown command code = 0, want non-zero")
	}
	if !strings.Contains(stderr.String(), `unknown command "nope"`) {
		t.Fatalf("stderr missing unknown command:\n%s", stderr.String())
	}
}

func TestDaemonURLDefaultUsesEnvironment(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz" {
			t.Fatalf("path = %s, want /healthz", r.URL.Path)
		}
		writeJSONForTest(t, w, map[string]any{"status": "ok"})
	}))
	t.Cleanup(server.Close)
	t.Setenv("OWRTD_URL", server.URL)
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := (&cli{stdout: &stdout, stderr: &stderr, client: server.Client()}).run([]string{"health"})
	if code != 0 {
		t.Fatalf("code = %d stderr=%q", code, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"status": "ok"`) {
		t.Fatalf("stdout missing health:\n%s", stdout.String())
	}
}

func TestClientRejectsInvalidDaemonURL(t *testing.T) {
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := (&cli{stdout: &stdout, stderr: &stderr, client: http.DefaultClient}).run(
		[]string{"--daemon-url", "//127.0.0.1:8765", "health"},
	)
	if code == 0 {
		t.Fatalf("code = 0, want non-zero")
	}
	if !strings.Contains(stderr.String(), "invalid daemon URL") {
		t.Fatalf("stderr missing invalid URL:\n%s", stderr.String())
	}
}

func TestHTTPErrorReturnsNonZero(t *testing.T) {
	_, stderr, code := runCLI(t, func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":"missing"}`, http.StatusNotFound)
	}, "status", "job_missing")

	if code == 0 {
		t.Fatalf("code = 0, want non-zero")
	}
	if !strings.Contains(stderr, "HTTP 404") || !strings.Contains(stderr, "missing") {
		t.Fatalf("stderr missing HTTP error detail:\n%s", stderr)
	}
}

func intPtr(v int) *int {
	return &v
}

func writeJSONForTest(t *testing.T, w http.ResponseWriter, value any) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(value); err != nil {
		t.Fatalf("encode response: %v", err)
	}
}
