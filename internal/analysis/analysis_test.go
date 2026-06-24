package analysis

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func writeReport(t *testing.T, report map[string]any) string {
	t.Helper()
	dir := t.TempDir()
	data, _ := json.Marshal(report)
	if err := os.WriteFile(filepath.Join(dir, "report.json"), data, 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestAnalyzeSuccess(t *testing.T) {
	dir := writeReport(t, map[string]any{
		"job_id": "job_ok", "state": "SUCCEEDED", "success": true, "dry_run": false,
	})
	a := Analyze(dir)
	v := a["verdict"].(map[string]any)
	if v["status"] != "succeeded" {
		t.Errorf("verdict = %v", v)
	}
	fs := a["findings"].([]map[string]any)
	if len(fs) != 1 || fs[0]["code"] != "job_succeeded" {
		t.Errorf("findings = %v", fs)
	}
	if a["ui_summary"].(map[string]any)["severity"] != "success" {
		t.Errorf("severity = %v", a["ui_summary"])
	}
}

func TestAnalyzeDryRun(t *testing.T) {
	dir := writeReport(t, map[string]any{"job_id": "job_dry", "state": "DRY_RUN", "dry_run": true})
	a := Analyze(dir)
	if a["verdict"].(map[string]any)["status"] != "planned" {
		t.Errorf("verdict = %v", a["verdict"])
	}
	if a["job"].(map[string]any)["result"] != "dry_run" {
		t.Errorf("result = %v", a["job"])
	}
}

func TestAnalyzeBuildDiskFull(t *testing.T) {
	dir := writeReport(t, map[string]any{
		"job_id": "job_fail", "state": "FAILED", "success": false,
		"build_summary": map[string]any{"classification": "disk_full", "success": false},
	})
	a := Analyze(dir)
	if a["verdict"].(map[string]any)["status"] != "failed" {
		t.Errorf("verdict = %v", a["verdict"])
	}
	foundDisk := false
	for _, f := range a["findings"].([]map[string]any) {
		if f["code"] == "build_disk_full" {
			foundDisk = true
		}
	}
	if !foundDisk {
		t.Errorf("expected build_disk_full finding: %v", a["findings"])
	}
	actions := a["next_actions"].([]string)
	if len(actions) < 2 {
		t.Errorf("expected disk-full next action: %v", actions)
	}
}

func TestAnalyzeRedactsWarnings(t *testing.T) {
	dir := writeReport(t, map[string]any{
		"job_id": "job_w", "success": false,
		"warnings": []any{"connect failed password=hunter2 retrying"},
	})
	a := Analyze(dir)
	for _, f := range a["findings"].([]map[string]any) {
		if s, _ := f["summary"].(string); contains(s, "hunter2") {
			t.Errorf("secret leaked into finding: %q", s)
		}
	}
}

func TestWriteFiles(t *testing.T) {
	dir := writeReport(t, map[string]any{"job_id": "job_w2", "success": true})
	a := Analyze(dir)
	jsonPath, mdPath, err := WriteFiles(dir, a)
	if err != nil {
		t.Fatalf("WriteFiles: %v", err)
	}
	if _, err := os.Stat(jsonPath); err != nil {
		t.Errorf("analysis.json missing: %v", err)
	}
	md, err := os.ReadFile(mdPath)
	if err != nil || len(md) == 0 {
		t.Fatalf("analysis.md: %v", err)
	}
	if string(md[:2]) != "# " {
		t.Errorf("analysis.md should start with heading")
	}
	// analysis.json must be valid JSON with the core keys.
	data, _ := os.ReadFile(jsonPath)
	var parsed map[string]any
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("analysis.json invalid: %v", err)
	}
	for _, k := range []string{"verdict", "findings", "next_actions", "guardrails", "job"} {
		if _, ok := parsed[k]; !ok {
			t.Errorf("analysis.json missing %q", k)
		}
	}
	// Guardrail: advisory only, no dangerous actions.
	g := parsed["guardrails"].(map[string]any)
	if g["advisory_only"] != true || g["dangerous_actions_allowed"] != false {
		t.Errorf("guardrails wrong: %v", g)
	}
}

func TestAnalyzeMissingReport(t *testing.T) {
	dir := t.TempDir() // no report.json
	a := Analyze(dir)
	// missing_report is an error finding, so the verdict is "failed"
	// (matches Python _verdict: first error finding wins).
	if a["verdict"].(map[string]any)["status"] != "failed" {
		t.Errorf("verdict = %v", a["verdict"])
	}
	foundMissing := false
	for _, f := range a["findings"].([]map[string]any) {
		if f["code"] == "missing_report" {
			foundMissing = true
		}
	}
	if !foundMissing {
		t.Errorf("expected missing_report finding: %v", a["findings"])
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
