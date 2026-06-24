// Package report is the Go port of python/owrt_monitor/reports.py. It writes the
// same report.json (sorted keys, 2-space indent, trailing newline) the daemon
// and the Python engine read, plus a human report.md. The JSON shape matches
// WorkflowReport.to_dict exactly so reports are interchangeable across engines.
package report

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/tonyandclaw/owrt_monitor/internal/artifact"
)

// Report mirrors reports.WorkflowReport.
type Report struct {
	JobID         string
	State         string
	Success       bool
	DryRun        bool
	RunDir        string
	Actions       []string
	Warnings      []string
	Artifact      *artifact.ExportedArtifact
	TestResults   []map[string]any
	BuildSummary  map[string]any
	BuildMetadata map[string]any
	Metrics       map[string]any
	DutStatus     map[string]any
	ScriptResults []map[string]any
	PytestResults []map[string]any
	SshResults    []map[string]any
}

// ToMap renders the report into the same dict shape as WorkflowReport.to_dict:
// all keys always present, lists default to [], optional objects to null.
func (r *Report) ToMap() map[string]any {
	m := map[string]any{
		"job_id":         r.JobID,
		"state":          r.State,
		"success":        r.Success,
		"dry_run":        r.DryRun,
		"run_dir":        r.RunDir,
		"actions":        listOrEmpty(r.Actions),
		"warnings":       listOrEmpty(r.Warnings),
		"artifact":       nil,
		"test_results":   mapsOrEmpty(r.TestResults),
		"build_summary":  nilOrMap(r.BuildSummary),
		"build_metadata": nilOrMap(r.BuildMetadata),
		"metrics":        nilOrMap(r.Metrics),
		"dut_status":     nilOrMap(r.DutStatus),
		"script_results": mapsOrEmpty(r.ScriptResults),
		"pytest_results": mapsOrEmpty(r.PytestResults),
		"ssh_results":    mapsOrEmpty(r.SshResults),
	}
	if r.Artifact != nil {
		m["artifact"] = map[string]any{
			"container_path": r.Artifact.ContainerPath,
			"host_path":      r.Artifact.HostPath,
			"filename":       r.Artifact.Filename,
			"size_bytes":     r.Artifact.SizeBytes,
			"sha256":         r.Artifact.SHA256,
		}
	}
	return m
}

// Write writes report.json and report.md into the run directory.
func (r *Report) Write() error {
	if err := os.MkdirAll(r.RunDir, 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(r.ToMap(), "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.WriteFile(filepath.Join(r.RunDir, "report.json"), data, 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(r.RunDir, "report.md"), []byte(r.markdown()), 0o644)
}

func (r *Report) markdown() string {
	var b strings.Builder
	fmt.Fprintf(&b, "# owrt_monitor job %s\n\n", r.JobID)
	fmt.Fprintf(&b, "- State: `%s`\n", r.State)
	fmt.Fprintf(&b, "- Success: `%t`\n", r.Success)
	fmt.Fprintf(&b, "- Dry run: `%t`\n", r.DryRun)
	fmt.Fprintf(&b, "- Run directory: `%s`\n", r.RunDir)

	if r.Artifact != nil {
		b.WriteString("\n## Artifact\n\n")
		fmt.Fprintf(&b, "- File: `%s`\n", r.Artifact.Filename)
		fmt.Fprintf(&b, "- Host path: `%s`\n", r.Artifact.HostPath)
		fmt.Fprintf(&b, "- Container path: `%s`\n", r.Artifact.ContainerPath)
		fmt.Fprintf(&b, "- Size bytes: `%d`\n", r.Artifact.SizeBytes)
		fmt.Fprintf(&b, "- SHA256: `%s`\n", r.Artifact.SHA256)
	}
	if len(r.Actions) > 0 {
		b.WriteString("\n## Actions\n\n")
		for _, a := range r.Actions {
			fmt.Fprintf(&b, "- %s\n", a)
		}
	}
	if len(r.Warnings) > 0 {
		b.WriteString("\n## Warnings\n\n")
		for _, w := range r.Warnings {
			fmt.Fprintf(&b, "- %s\n", w)
		}
	}
	if len(r.TestResults) > 0 {
		passed, skipped := 0, 0
		for _, t := range r.TestResults {
			if b, _ := t["skipped"].(bool); b {
				skipped++
			} else if p, _ := t["passed"].(bool); p {
				passed++
			}
		}
		total := len(r.TestResults)
		verdict := "PASS"
		if total-passed-skipped > 0 {
			verdict = "FAIL"
		}
		b.WriteString("\n## Smoke Tests\n\n")
		fmt.Fprintf(&b, "- Result: **%s** (%d/%d passed, %d skipped)\n", verdict, passed, total, skipped)
	}
	return b.String()
}

func listOrEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func mapsOrEmpty(s []map[string]any) []map[string]any {
	if s == nil {
		return []map[string]any{}
	}
	return s
}

func nilOrMap(m map[string]any) any {
	if m == nil {
		return nil
	}
	return m
}
