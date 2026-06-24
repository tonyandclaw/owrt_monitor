// Package analysis is a Go port of python/owrt_monitor/analysis.py's advisory
// path. It reads already-persisted job artifacts (report.json, build.log) and
// produces a deterministic, redacted analysis bundle (analysis.json + .md) — a
// structured input for a future LLM/UI layer. It is advisory only: it never
// chooses firmware, runs sysupgrade, or mutates anything.
//
// This is a focused port: it covers the decision-relevant fields (job,
// guardrails, ui_summary, verdict, build, findings, next_actions). The heavier
// bug-report draft, per-file hashes, and evidence tails from the Python version
// are intentionally omitted.
package analysis

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/tonyandclaw/owrt_monitor/internal/buildlog"
)

var secretRedactions = []struct {
	re   *regexp.Regexp
	repl string
}{
	{regexp.MustCompile(`(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key)(\s*[:=]\s*)([^\s,;]+)`), "${1}${2}<redacted>"},
	{regexp.MustCompile(`(?i)\b(authorization\s*:\s*)(bearer|basic)\s+[^\s,;]+`), "${1}<redacted>"},
}

func redact(s string) string {
	for _, r := range secretRedactions {
		s = r.re.ReplaceAllString(s, r.repl)
	}
	return s
}

// Analyze builds the advisory bundle for a run directory
// (analysis.analyze_run_dir, focused subset).
func Analyze(runDir string) map[string]any {
	abs, err := filepath.Abs(runDir)
	if err != nil {
		abs = runDir
	}
	report := loadJSON(filepath.Join(abs, "report.json"))
	buildSummary := buildSummaryFor(abs, report)
	findings := findings(report, buildSummary, abs)
	nextActions := nextActions(report, buildSummary, findings)
	verdict := verdict(report, buildSummary, findings)

	jobID, _ := report["job_id"].(string)
	if jobID == "" {
		jobID = filepath.Base(abs)
	}

	return map[string]any{
		"schema_version": 1,
		"kind":           "advisory_analysis",
		"generated_at":   time.Now().UTC().Format("2006-01-02T15:04:05.000000-07:00"),
		"job": map[string]any{
			"job_id":  jobID,
			"run_dir": abs,
			"state":   report["state"],
			"success": report["success"],
			"dry_run": report["dry_run"],
			"result":  jobResult(report),
		},
		"guardrails": map[string]any{
			"advisory_only":             true,
			"dangerous_actions_allowed": false,
			"input_policy":              "structured_redacted_artifacts_only",
			"workflow_authority":        "deterministic workflow and config",
			"approval_required_for": []string{
				"sysupgrade", "bootloader environment changes",
				"deleting build directories", "network changes on DUT",
			},
		},
		"ui_summary": map[string]any{
			"severity": uiSeverity(report, findings),
			"title":    verdict["summary"],
			"badges":   uiBadges(report, buildSummary, findings),
		},
		"verdict":      verdict,
		"build":        buildSummary,
		"findings":     findings,
		"next_actions": nextActions,
	}
}

// WriteFiles writes analysis.json (sorted keys, 2-space indent) and analysis.md
// into the run directory (analysis.write_analysis_files).
func WriteFiles(runDir string, analysis map[string]any) (string, string, error) {
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		return "", "", err
	}
	jsonPath := filepath.Join(runDir, "analysis.json")
	mdPath := filepath.Join(runDir, "analysis.md")
	data, err := json.MarshalIndent(analysis, "", "  ")
	if err != nil {
		return "", "", err
	}
	if err := os.WriteFile(jsonPath, append(data, '\n'), 0o644); err != nil {
		return "", "", err
	}
	if err := os.WriteFile(mdPath, []byte(renderMarkdown(analysis)), 0o644); err != nil {
		return "", "", err
	}
	return jsonPath, mdPath, nil
}

func renderMarkdown(a map[string]any) string {
	job, _ := a["job"].(map[string]any)
	verdict, _ := a["verdict"].(map[string]any)
	guard, _ := a["guardrails"].(map[string]any)
	var b strings.Builder
	fmt.Fprintf(&b, "# owrt_monitor advisory analysis %v\n\n", strOr(job["job_id"], ""))
	fmt.Fprintf(&b, "- Verdict: `%v`\n", strOr(verdict["status"], "unknown"))
	fmt.Fprintf(&b, "- Summary: %v\n", strOr(verdict["summary"], "No summary available."))
	fmt.Fprintf(&b, "- Advisory only: `%v`\n", guard["advisory_only"])
	fmt.Fprintf(&b, "- Workflow authority: `%v`\n", strOr(guard["workflow_authority"], ""))

	if fs, ok := a["findings"].([]map[string]any); ok && len(fs) > 0 {
		b.WriteString("\n## Findings\n\n")
		for _, f := range fs {
			fmt.Fprintf(&b, "- `%v` %v\n", strOr(f["severity"], "info"), strOr(f["summary"], ""))
		}
	}
	if as, ok := a["next_actions"].([]string); ok && len(as) > 0 {
		b.WriteString("\n## Next Actions\n\n")
		for _, action := range as {
			fmt.Fprintf(&b, "- %s\n", action)
		}
	}
	return b.String()
}

func buildSummaryFor(runDir string, report map[string]any) map[string]any {
	if bs, ok := report["build_summary"].(map[string]any); ok {
		return bs
	}
	buildLog := filepath.Join(runDir, "build.log")
	if _, err := os.Stat(buildLog); err == nil {
		return buildlog.Classify(buildLog).ToMap()
	}
	return nil
}

func findings(report, buildSummary map[string]any, runDir string) []map[string]any {
	var out []map[string]any
	if truthy(report["dry_run"]) {
		out = append(out, finding("dry_run_only", "info",
			"This job was a dry-run; no build, flash, or DUT mutation was executed."))
	}
	if buildSummary != nil {
		cls := strOr(buildSummary["classification"], "unknown")
		switch {
		case cls == "disk_full":
			out = append(out, finding("build_disk_full", "error",
				"OpenWrt build failed because the builder ran out of disk space."))
		case cls == "failed_package":
			pkg := strOr(buildSummary["failed_package"], strOr(buildSummary["failed_step"], ""))
			out = append(out, finding("build_failed_package", "error",
				fmt.Sprintf("OpenWrt build failed inside `%s`.", pkg)))
		case cls == "compile_error" || cls == "unknown" || cls == "missing_log" || cls == "unreadable_log":
			out = append(out, finding("build_"+cls, "error",
				fmt.Sprintf("Build did not succeed; classifier returned `%s`.", cls)))
		}
	}
	for i, w := range listOf(report["warnings"]) {
		if i >= 10 {
			break
		}
		text := fmt.Sprintf("%v", w)
		code, severity := "workflow_warning", "warning"
		lower := strings.ToLower(text)
		if strings.Contains(lower, "failed to boot") || strings.Contains(lower, "boot failure") {
			code, severity = "dut_boot_failure", "error"
		}
		out = append(out, finding(code, severity, redact(text)))
	}
	for _, lk := range []struct{ label, key string }{
		{"smoke", "test_results"}, {"script", "script_results"},
		{"pytest", "pytest_results"}, {"ssh", "ssh_results"},
	} {
		failed := 0
		for _, r := range listOf(report[lk.key]) {
			rm, ok := r.(map[string]any)
			if !ok {
				continue
			}
			if !truthy(rm["passed"]) && !truthy(rm["skipped"]) {
				failed++
			}
		}
		if failed > 0 {
			out = append(out, finding(lk.label+"_test_failed", "error",
				fmt.Sprintf("%d %s test result(s) failed.", failed, lk.label)))
		}
	}
	if len(out) == 0 && report["success"] == true {
		out = append(out, finding("job_succeeded", "info",
			"Job completed successfully according to report.json."))
	} else if len(out) == 0 {
		if _, err := os.Stat(filepath.Join(runDir, "report.json")); err != nil {
			out = append(out, finding("missing_report", "error",
				"No report.json was found for this run directory."))
		}
	}
	return out
}

func nextActions(report, buildSummary map[string]any, findings []map[string]any) []string {
	actions := []string{"Treat this analysis as advisory; rerun the deterministic command before acting."}
	codes := map[string]bool{}
	for _, f := range findings {
		codes[strOr(f["code"], "")] = true
	}
	cls := ""
	if buildSummary != nil {
		cls = strOr(buildSummary["classification"], "")
	}
	if codes["build_disk_full"] {
		actions = append(actions, "Free space in the builder container/host volume, then rerun the build.")
	}
	if codes["build_failed_package"] {
		pkg := ""
		if buildSummary != nil {
			pkg = strOr(buildSummary["failed_package"], "")
		}
		if pkg != "" {
			actions = append(actions, fmt.Sprintf("Inspect the failing package `%s` and rerun with verbose logs.", pkg))
		} else {
			actions = append(actions, "Inspect the failing make step and rerun with verbose logs.")
		}
	}
	if cls == "compile_error" || cls == "unknown" {
		actions = append(actions, "Open build.log and runner.output.jsonl around the final error lines.")
	}
	if codes["dut_boot_failure"] {
		actions = append(actions, "Inspect serial.log around the boot failure evidence before retrying flash.")
	}
	for code := range codes {
		if strings.HasSuffix(code, "_test_failed") {
			actions = append(actions, "Inspect failed test output in report.json, then rerun `owrt-monitor test`.")
			break
		}
	}
	if truthy(report["dry_run"]) {
		actions = append(actions, "When the lab is ready, rerun without `--dry-run` and keep explicit guards.")
	}
	if report["success"] != true && !truthy(report["dry_run"]) {
		actions = append(actions, "Do not run destructive flash based only on this analysis.")
	}
	return actions
}

func verdict(report, buildSummary map[string]any, findings []map[string]any) map[string]any {
	if truthy(report["dry_run"]) {
		return map[string]any{"status": "planned", "summary": "Dry-run completed; no mutation was executed."}
	}
	if report["success"] == true {
		return map[string]any{"status": "succeeded", "summary": "Job succeeded."}
	}
	for _, f := range findings {
		if strOr(f["severity"], "") == "error" {
			return map[string]any{"status": "failed", "summary": strOr(f["summary"], "")}
		}
	}
	if buildSummary != nil && buildSummary["success"] == false {
		return map[string]any{"status": "failed",
			"summary": fmt.Sprintf("Build classifier returned `%s`.", strOr(buildSummary["classification"], ""))}
	}
	return map[string]any{"status": "unknown", "summary": "Not enough structured data to classify the job."}
}

func uiSeverity(report map[string]any, findings []map[string]any) string {
	for _, f := range findings {
		if strOr(f["severity"], "") == "error" {
			return "error"
		}
	}
	for _, f := range findings {
		if strOr(f["severity"], "") == "warning" {
			return "warning"
		}
	}
	if truthy(report["dry_run"]) {
		return "info"
	}
	if report["success"] == true {
		return "success"
	}
	return "unknown"
}

func uiBadges(report, buildSummary map[string]any, findings []map[string]any) []string {
	badges := []string{}
	if s := strOr(report["state"], ""); s != "" {
		badges = append(badges, s)
	}
	if truthy(report["dry_run"]) {
		badges = append(badges, "dry-run")
	}
	if buildSummary != nil {
		if c := strOr(buildSummary["classification"], ""); c != "" {
			badges = append(badges, "build:"+c)
		}
	}
	for _, f := range findings {
		if strOr(f["code"], "") == "dut_boot_failure" {
			badges = append(badges, "dut:boot-failure")
			break
		}
	}
	return badges
}

func jobResult(report map[string]any) any {
	if truthy(report["dry_run"]) {
		return "dry_run"
	}
	if report["success"] == true {
		return "success"
	}
	if report["success"] == false {
		return "failed"
	}
	return nil
}

func finding(code, severity, summary string) map[string]any {
	return map[string]any{"code": code, "severity": severity, "summary": summary}
}

func loadJSON(path string) map[string]any {
	data, err := os.ReadFile(path)
	if err != nil {
		return map[string]any{}
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return map[string]any{}
	}
	return out
}

func truthy(v any) bool {
	switch t := v.(type) {
	case bool:
		return t
	case string:
		return t != ""
	case nil:
		return false
	default:
		return true
	}
}

func strOr(v any, fallback string) string {
	if s, ok := v.(string); ok && s != "" {
		return s
	}
	return fallback
}

func listOf(v any) []any {
	if l, ok := v.([]any); ok {
		return l
	}
	return nil
}
