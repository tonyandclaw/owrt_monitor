package workflow

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/tonyandclaw/owrt_monitor/internal/artifact"
	"github.com/tonyandclaw/owrt_monitor/internal/store"
)

// fakeDocker is the test BuildClient — fabricates a build log and firmware file
// without touching docker (mirrors Python's FakeDockerBuildClient).
type fakeDocker struct {
	candidates   []artifact.Candidate
	contents     []byte
	preflightErr error
	buildErr     error
}

func (f *fakeDocker) BuildCommand(redactEnv bool) []string {
	return []string{"docker", "exec", "testbuilder", "make", "target"}
}
func (f *fakeDocker) Preflight() error { return f.preflightErr }
func (f *fakeDocker) RunBuild(ctx context.Context, logPath string) error {
	if f.buildErr != nil {
		return f.buildErr
	}
	return os.WriteFile(logPath, []byte("make[1]: Entering directory\nBuild complete.\n"), 0o644)
}
func (f *fakeDocker) ListArtifacts(patterns []string) ([]artifact.Candidate, error) {
	return f.candidates, nil
}
func (f *fakeDocker) GatherBuildMetadata() map[string]any {
	return map[string]any{"git_commit": "abc123", "git_describe": "v1-dirty", "git_dirty": true}
}
func (f *fakeDocker) CopyArtifact(c artifact.Candidate, hostPath string) (artifact.ExportedArtifact, error) {
	if err := os.MkdirAll(filepath.Dir(hostPath), 0o755); err != nil {
		return artifact.ExportedArtifact{}, err
	}
	if err := os.WriteFile(hostPath, f.contents, 0o644); err != nil {
		return artifact.ExportedArtifact{}, err
	}
	info, _ := os.Stat(hostPath)
	return artifact.ExportedArtifact{
		ContainerPath: "/work/" + c.Path,
		HostPath:      hostPath,
		Filename:      filepath.Base(hostPath),
		SizeBytes:     info.Size(),
		SHA256:        "fakesha256deadbeef",
	}, nil
}

const testConfig = `
project:
  artifact_dir: .
builder:
  container: testbuilder
  workdir: /work
  command: [make, target]
artifact:
  patterns: ["bin/targets/**/openwrt-*-sysupgrade.bin"]
  selection: newest
`

func writeConfig(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(testConfig), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func newFake() *fakeDocker {
	return &fakeDocker{
		candidates: []artifact.Candidate{
			{Path: "bin/targets/x/openwrt-test-sysupgrade.bin", SizeBytes: 5 << 20, Mtime: 1000},
		},
		contents: make([]byte, 5<<20),
	}
}

func TestBuildWorkflowFullPath(t *testing.T) {
	cfgPath := writeConfig(t)
	wf, err := NewBuildWorkflow(cfgPath, nil, newFake())
	if err != nil {
		t.Fatalf("NewBuildWorkflow: %v", err)
	}
	defer wf.Close()

	rep, err := wf.Run(false, false)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.State != "SUCCEEDED" || !rep.Success {
		t.Fatalf("report state = %s success = %v", rep.State, rep.Success)
	}
	if rep.Artifact == nil || rep.Artifact.Filename != "openwrt-test-sysupgrade.bin" {
		t.Fatalf("artifact = %+v", rep.Artifact)
	}

	// report.json written and well-formed.
	data, err := os.ReadFile(filepath.Join(rep.RunDir, "report.json"))
	if err != nil {
		t.Fatalf("read report.json: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("report.json invalid: %v", err)
	}
	if got["state"] != "SUCCEEDED" {
		t.Errorf("report.json state = %v", got["state"])
	}

	// events.jsonl contains the expected state_transition sequence.
	states := readTransitionStates(t, filepath.Join(rep.RunDir, "events.jsonl"))
	wantSeq := []string{"PREFLIGHT", "BUILD_RUNNING", "BUILD_SUCCEEDED", "ARTIFACT_SELECTED", "ARTIFACT_EXPORTED"}
	for _, want := range wantSeq {
		if !contains(states, want) {
			t.Errorf("missing state transition %q in %v", want, states)
		}
	}

	// Store reflects the finished job + recorded artifact + released lock.
	st, err := store.Open(wf.config.StateDBPath(cfgPath))
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer st.Close()
	job, err := st.GetJob(rep.JobID)
	if err != nil || job == nil {
		t.Fatalf("GetJob: %v %v", job, err)
	}
	if job.State != "SUCCEEDED" || job.Result != "success" {
		t.Errorf("job row = state %s result %s", job.State, job.Result)
	}
	if owner, held, _ := st.BuilderLockOwner("testbuilder"); held {
		t.Errorf("builder lock should be released, held by %s", owner)
	}
}

func TestBuildWorkflowDryRun(t *testing.T) {
	wf, err := NewBuildWorkflow(writeConfig(t), nil, newFake())
	if err != nil {
		t.Fatalf("NewBuildWorkflow: %v", err)
	}
	defer wf.Close()
	rep, err := wf.Run(true, false)
	if err != nil {
		t.Fatalf("Run dry: %v", err)
	}
	if rep.State != "DRY_RUN" || !rep.Success {
		t.Errorf("dry-run report = %s / %v", rep.State, rep.Success)
	}
	// No build.log on dry-run (returns before building).
	if _, err := os.Stat(filepath.Join(rep.RunDir, "build.log")); !os.IsNotExist(err) {
		t.Errorf("dry-run should not produce build.log")
	}
}

func TestBuildWorkflowRejectsFlash(t *testing.T) {
	wf, err := NewBuildWorkflow(writeConfig(t), nil, newFake())
	if err != nil {
		t.Fatalf("NewBuildWorkflow: %v", err)
	}
	defer wf.Close()
	if _, err := wf.Run(false, true); err == nil {
		t.Error("allow_flash should be rejected until the DUT path is ported")
	}
}

func TestBuildWorkflowBuilderBusy(t *testing.T) {
	cfgPath := writeConfig(t)
	wf, err := NewBuildWorkflow(cfgPath, nil, newFake())
	if err != nil {
		t.Fatalf("NewBuildWorkflow: %v", err)
	}
	defer wf.Close()
	// Pre-acquire the builder lock as a different job.
	if ok, err := wf.store.AcquireBuilderLock("testbuilder", "other-job", nil); err != nil || !ok {
		t.Fatalf("seed lock: %v %v", ok, err)
	}
	rep, err := wf.Run(false, false)
	if err == nil {
		t.Fatal("Run should fail when builder is busy")
	}
	if rep == nil || rep.State != "FAILED" {
		t.Errorf("expected FAILED report, got %+v", rep)
	}
}

func readTransitionStates(t *testing.T, path string) []string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open events.jsonl: %v", err)
	}
	defer f.Close()
	var states []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var obj map[string]any
		if json.Unmarshal(sc.Bytes(), &obj) != nil {
			continue
		}
		if obj["event"] != "state_transition" {
			continue
		}
		if fields, ok := obj["fields"].(map[string]any); ok {
			if s, ok := fields["state"].(string); ok {
				states = append(states, s)
			}
		}
	}
	return states
}

func contains(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}
