package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const miniConfig = `
project:
  artifact_dir: .
builder:
  container: testbuilder
  workdir: /work
  command: [make, target]
artifact:
  patterns: ["bin/targets/**/openwrt-*-sysupgrade.bin"]
`

func newCLI() (*cli, *bytes.Buffer, *bytes.Buffer) {
	var out, errb bytes.Buffer
	return &cli{stdout: &out, stderr: &errb}, &out, &errb
}

func writeMini(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(miniConfig), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func examplePath() string { return filepath.Join("..", "..", "configs", "example.yaml") }

func TestValidateExample(t *testing.T) {
	c, out, _ := newCLI()
	if code := c.run([]string{"validate", "--config", examplePath()}); code != 0 {
		t.Fatalf("validate exit = %d", code)
	}
	if !strings.Contains(out.String(), "OK") || !strings.Contains(out.String(), "ap-be5000") {
		t.Errorf("validate stdout = %q", out.String())
	}
}

func TestValidateMissingConfig(t *testing.T) {
	c, _, _ := newCLI()
	if code := c.run([]string{"validate"}); code != 2 {
		t.Errorf("validate without --config exit = %d, want 2", code)
	}
}

func TestValidateBadConfig(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bad.yaml")
	_ = os.WriteFile(path, []byte("builder: {container: c}\n"), 0o644) // missing workdir/command/artifact
	c, _, _ := newCLI()
	if code := c.run([]string{"validate", "--config", path}); code != 1 {
		t.Errorf("validate bad config exit = %d, want 1", code)
	}
}

func TestDryRunThenStatus(t *testing.T) {
	cfg := writeMini(t)

	c, out, errb := newCLI()
	if code := c.run([]string{"dry-run", "--config", cfg}); code != 0 {
		t.Fatalf("dry-run exit = %d, stderr=%s", code, errb.String())
	}
	if !strings.Contains(out.String(), "DRY_RUN") {
		t.Errorf("dry-run stdout = %q", out.String())
	}

	// status should now list the dry-run job from the shared store.
	c2, out2, errb2 := newCLI()
	if code := c2.run([]string{"status", "--config", cfg}); code != 0 {
		t.Fatalf("status exit = %d, stderr=%s", code, errb2.String())
	}
	if !strings.Contains(out2.String(), "DRY_RUN") || !strings.Contains(out2.String(), "JOB ID") {
		t.Errorf("status stdout = %q", out2.String())
	}
}

func TestStatusEmpty(t *testing.T) {
	c, out, _ := newCLI()
	if code := c.run([]string{"status", "--config", writeMini(t)}); code != 0 {
		t.Fatalf("status exit = %d", code)
	}
	if !strings.Contains(out.String(), "no jobs") {
		t.Errorf("empty status stdout = %q", out.String())
	}
}

func TestBuildRejectsFlash(t *testing.T) {
	c, _, errb := newCLI()
	code := c.run([]string{"build", "--config", writeMini(t), "--allow-flash"})
	if code != 1 {
		t.Errorf("build --allow-flash exit = %d, want 1", code)
	}
	if !strings.Contains(errb.String(), "flash") {
		t.Errorf("expected flash-not-implemented message, got %q", errb.String())
	}
}

func TestUnknownCommand(t *testing.T) {
	c, _, _ := newCLI()
	if code := c.run([]string{"frobnicate"}); code != 2 {
		t.Errorf("unknown command exit = %d, want 2", code)
	}
	if code := c.run(nil); code != 2 {
		t.Errorf("no args exit = %d, want 2", code)
	}
}

func TestAnalyzeRunDir(t *testing.T) {
	cfg := writeMini(t)
	// Produce a job + report via dry-run, then analyze its run dir.
	c, out, _ := newCLI()
	if code := c.run([]string{"dry-run", "--config", cfg}); code != 0 {
		t.Fatalf("dry-run exit")
	}
	// Extract run dir from the dry-run output ("run dir:  <path>").
	runDir := ""
	for _, line := range strings.Split(out.String(), "\n") {
		if strings.HasPrefix(line, "run dir:") {
			runDir = strings.TrimSpace(strings.TrimPrefix(line, "run dir:"))
		}
	}
	if runDir == "" {
		t.Fatalf("could not find run dir in: %q", out.String())
	}

	c2, out2, errb2 := newCLI()
	if code := c2.run([]string{"analyze", "--run-dir", runDir}); code != 0 {
		t.Fatalf("analyze exit, stderr=%s", errb2.String())
	}
	if !strings.Contains(out2.String(), "verdict:") || !strings.Contains(out2.String(), "analysis.json") {
		t.Errorf("analyze stdout = %q", out2.String())
	}
	if _, err := os.Stat(filepath.Join(runDir, "analysis.json")); err != nil {
		t.Errorf("analysis.json not written: %v", err)
	}
}

func TestAnalyzeRequiresTarget(t *testing.T) {
	c, _, _ := newCLI()
	if code := c.run([]string{"analyze"}); code != 2 {
		t.Errorf("analyze without target exit = %d, want 2", code)
	}
}

func TestDiffCommand(t *testing.T) {
	dir := t.TempDir()
	from := filepath.Join(dir, "from.yaml")
	to := filepath.Join(dir, "to.yaml")
	_ = os.WriteFile(from, []byte(miniConfig), 0o644)
	// Change the builder command in the second config.
	_ = os.WriteFile(to, []byte(strings.Replace(miniConfig, "[make, target]", "[make, other]", 1)), 0o644)

	c, out, errb := newCLI()
	code := c.run([]string{"diff", "--from", from, "--to", to})
	if code != 0 {
		t.Fatalf("diff exit = %d, stderr=%s", code, errb.String())
	}
	if !strings.Contains(out.String(), "change(s)") {
		t.Errorf("diff stdout = %q", out.String())
	}

	// Identical files → no differences.
	c2, out2, _ := newCLI()
	if code := c2.run([]string{"diff", "--from", from, "--to", from}); code != 0 {
		t.Fatalf("diff identical exit = %d", code)
	}
	if !strings.Contains(out2.String(), "no differences") {
		t.Errorf("expected no differences, got %q", out2.String())
	}
}

func TestHelp(t *testing.T) {
	c, out, _ := newCLI()
	if code := c.run([]string{"--help"}); code != 0 {
		t.Errorf("--help exit = %d", code)
	}
	if !strings.Contains(out.String(), "owrt-engine") {
		t.Errorf("help missing banner: %q", out.String())
	}
}
