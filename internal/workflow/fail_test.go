package workflow

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/tonyandclaw/owrt_monitor/internal/artifact"
)

func runFake(t *testing.T, f *fakeDocker) (*BuildWorkflow, error) {
	t.Helper()
	wf, err := NewBuildWorkflow(writeConfig(t), nil, f)
	if err != nil {
		t.Fatalf("NewBuildWorkflow: %v", err)
	}
	t.Cleanup(func() { _ = wf.Close() })
	_, runErr := wf.Run(false, false)
	return wf, runErr
}

func assertFailed(t *testing.T, wf *BuildWorkflow, runErr error) {
	t.Helper()
	if runErr == nil {
		t.Fatal("expected Run to fail")
	}
	// The job must be persisted as FAILED and the builder lock released.
	jobs, err := wf.store.RecentJobs(1)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("RecentJobs = %d, err %v", len(jobs), err)
	}
	if jobs[0].State != "FAILED" || jobs[0].Result != "failed" {
		t.Errorf("job not marked FAILED: %+v", jobs[0])
	}
	if owner, held, _ := wf.store.BuilderLockOwner("testbuilder"); held {
		t.Errorf("builder lock leaked, held by %s", owner)
	}
	// report.json on disk reflects the failure.
	data, err := os.ReadFile(filepath.Join(jobs[0].ArtifactDir, "report.json"))
	if err != nil {
		t.Fatalf("read report.json: %v", err)
	}
	if len(data) == 0 {
		t.Fatal("empty report.json")
	}
}

func TestBuildFailureMarksFailed(t *testing.T) {
	f := newFake()
	f.buildErr = errors.New("make exited 2")
	wf, err := runFake(t, f)
	assertFailed(t, wf, err)
}

func TestPreflightFailureMarksFailed(t *testing.T) {
	f := newFake()
	f.preflightErr = errors.New("container not running")
	wf, err := runFake(t, f)
	assertFailed(t, wf, err)
}

func TestNoArtifactMarksFailed(t *testing.T) {
	f := newFake()
	f.candidates = []artifact.Candidate{} // nothing matches → selection error
	wf, err := runFake(t, f)
	assertFailed(t, wf, err)
}

func TestProfileAppliedFromDefault(t *testing.T) {
	// A config with a default_profile should apply it at construction.
	dir := t.TempDir()
	cfg := filepath.Join(dir, "config.yaml")
	body := `
project:
  artifact_dir: .
  default_profile: board-a
builder:
  container: testbuilder
  workdir: /work
  command: [make]
artifact:
  patterns: ["x"]
profiles:
  board-a:
    builder:
      command: [make, board-a-target]
`
	if err := os.WriteFile(cfg, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	wf, err := NewBuildWorkflow(cfg, nil, newFake())
	if err != nil {
		t.Fatalf("NewBuildWorkflow: %v", err)
	}
	defer wf.Close()
	if wf.profile == nil || *wf.profile != "board-a" {
		t.Fatalf("effective profile = %v, want board-a", wf.profile)
	}
	if len(wf.config.Builder.Command) != 2 || wf.config.Builder.Command[1] != "board-a-target" {
		t.Errorf("profile overlay not applied: %v", wf.config.Builder.Command)
	}
}
