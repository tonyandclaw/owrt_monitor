package report

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/tonyandclaw/owrt_monitor/internal/artifact"
)

func TestWriteReportJSONShape(t *testing.T) {
	dir := t.TempDir()
	r := &Report{
		JobID:   "job_x",
		State:   "SUCCEEDED",
		Success: true,
		DryRun:  false,
		RunDir:  dir,
		Actions: []string{"built", "flashed"},
		Artifact: &artifact.ExportedArtifact{
			ContainerPath: "/c/fw.bin", HostPath: "/h/fw.bin",
			Filename: "fw.bin", SizeBytes: 4 << 20, SHA256: "deadbeef",
		},
		TestResults: []map[string]any{
			{"command": "ubus call system board", "passed": true, "duration_sec": 0.4},
		},
		Metrics: map[string]any{"total_duration_sec": 12.5},
	}
	if err := r.Write(); err != nil {
		t.Fatalf("Write: %v", err)
	}

	data, err := os.ReadFile(filepath.Join(dir, "report.json"))
	if err != nil {
		t.Fatalf("read report.json: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("report.json invalid: %v", err)
	}
	// All to_dict keys must be present (Python asdict always emits them).
	for _, k := range []string{
		"job_id", "state", "success", "dry_run", "run_dir", "actions", "warnings",
		"artifact", "test_results", "build_summary", "build_metadata", "metrics",
		"dut_status", "script_results", "pytest_results", "ssh_results",
	} {
		if _, ok := got[k]; !ok {
			t.Errorf("report.json missing key %q", k)
		}
	}
	// Unset optionals are null; unset lists are [].
	if got["build_summary"] != nil {
		t.Errorf("build_summary should be null, got %v", got["build_summary"])
	}
	if w, ok := got["warnings"].([]any); !ok || len(w) != 0 {
		t.Errorf("warnings should be [], got %v", got["warnings"])
	}
	art := got["artifact"].(map[string]any)
	if art["filename"] != "fw.bin" || art["sha256"] != "deadbeef" {
		t.Errorf("artifact shape wrong: %v", art)
	}

	// report.md exists and has the header.
	md, err := os.ReadFile(filepath.Join(dir, "report.md"))
	if err != nil {
		t.Fatalf("read report.md: %v", err)
	}
	if len(md) == 0 || string(md[:2]) != "# " {
		t.Errorf("report.md should start with a heading")
	}
}
