package docker

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tonyandclaw/owrt_monitor/internal/config"
)

func TestBuildCommandSortsEnvAndRedacts(t *testing.T) {
	c := New(config.BuilderConfig{
		Container: "openwrtbuild",
		Workdir:   "/work",
		Command:   []string{"make", "target"},
		Env:       map[string]string{"ZED": "1", "ALPHA": "secret"},
	})
	cmd := c.BuildCommand(false)
	want := []string{
		"docker", "exec", "--workdir", "/work",
		"-e", "ALPHA=secret", "-e", "ZED=1",
		"openwrtbuild", "make", "target",
	}
	if strings.Join(cmd, " ") != strings.Join(want, " ") {
		t.Errorf("BuildCommand =\n %v\nwant\n %v", cmd, want)
	}
	red := c.BuildCommand(true)
	if !contains(red, "ALPHA=<redacted>") || !contains(red, "ZED=<redacted>") {
		t.Errorf("redacted command should mask values: %v", red)
	}
}

func TestParseArtifactLines(t *testing.T) {
	out := "5242880\t1700000000\tbin/a.bin\n10485760\t1700000100\tbin/b.bin\n"
	cands, err := parseArtifactLines(out)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(cands) != 2 {
		t.Fatalf("got %d candidates", len(cands))
	}
	if cands[0].Path != "bin/a.bin" || cands[0].SizeBytes != 5242880 || cands[0].Mtime != 1.7e9 {
		t.Errorf("candidate[0] = %+v", cands[0])
	}
}

func TestParseArtifactLinesDedupAndBlank(t *testing.T) {
	out := "1\t2\tx.bin\n\n3\t4\tx.bin\n"
	cands, err := parseArtifactLines(out)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(cands) != 1 || cands[0].SizeBytes != 3 {
		t.Errorf("dedup should keep last write: %+v", cands)
	}
}

func TestParseArtifactLinesErrors(t *testing.T) {
	if _, err := parseArtifactLines("only\ttwo\n"); err == nil {
		t.Error("malformed line (2 fields) should error")
	}
	if _, err := parseArtifactLines("notanumber\t2\tx.bin\n"); err == nil {
		t.Error("non-numeric size should error")
	}
}

func TestSHA256File(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "f")
	if err := os.WriteFile(path, []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := SHA256File(path)
	if err != nil {
		t.Fatalf("SHA256File: %v", err)
	}
	// echo -n abc | sha256sum
	want := "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	if got != want {
		t.Errorf("SHA256File = %s, want %s", got, want)
	}
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}
