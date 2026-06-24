// Package buildlog is the Go port of python/owrt_monitor/build_log.py: it
// classifies an OpenWrt build.log into a structured summary (success /
// disk_full / failed_package / compile_error / unknown) with evidence,
// warnings, and a parsed duration. The summary shape matches BuildLogSummary
// .to_dict so reports are identical across engines.
package buildlog

import (
	"os"
	"regexp"
	"strconv"
	"strings"
)

var (
	successDone  = regexp.MustCompile(`^>>>> (?P<target>\S+)\s+Build done in:\s+(?P<duration>[\d:.]+)\s*$`)
	diskFull     = regexp.MustCompile(`(?i)No space left on device`)
	toplevelFail = regexp.MustCompile(`^make:\s+\*\*\*\s+\[(?P<makefile>[^\]]+):\s*(?P<target>\S+)\]\s+Error\s+\d+\s*$`)
	packageFail  = regexp.MustCompile(`^make\[\d+\]:\s+\*\*\*\s+\[(?P<step>[^\]]+)\]\s+Error\s+\d+\s*$`)
	warningRe    = regexp.MustCompile(`^WARNING:\s+(?P<message>.*)$`)
	knownNoise   = []*regexp.Regexp{
		regexp.MustCompile(`^fatal: No names found, cannot describe anything\.\s*$`),
		regexp.MustCompile(`^cat: write error: No space left on device\s*$`),
	}
)

const (
	maxWarnings     = 50
	maxEvidenceLine = 5
)

// Summary mirrors build_log.BuildLogSummary.
type Summary struct {
	Classification string
	Success        bool
	DurationSec    *float64
	FailedTarget   string
	FailedStep     string
	FailedPackage  string
	Evidence       []string
	Warnings       []string
}

// ToMap renders the summary into BuildLogSummary.to_dict's shape (nullable
// fields become null, list fields default to []).
func (s Summary) ToMap() map[string]any {
	return map[string]any{
		"classification": s.Classification,
		"success":        s.Success,
		"duration_sec":   floatOrNil(s.DurationSec),
		"failed_target":  strOrNil(s.FailedTarget),
		"failed_step":    strOrNil(s.FailedStep),
		"failed_package": strOrNil(s.FailedPackage),
		"evidence":       listOrEmpty(s.Evidence),
		"warnings":       listOrEmpty(s.Warnings),
	}
}

// Classify reads and classifies a build.log (build_log.classify_build_log).
func Classify(logPath string) Summary {
	data, err := os.ReadFile(logPath)
	if err != nil {
		if os.IsNotExist(err) {
			return Summary{Classification: "missing_log", Success: false}
		}
		return Summary{Classification: "unreadable_log", Success: false, Evidence: []string{err.Error()}}
	}
	lines := strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n")
	// splitlines() drops a trailing empty element from a final newline.
	if n := len(lines); n > 0 && lines[n-1] == "" {
		lines = lines[:n-1]
	}

	var (
		successM, toplevelM, packageM []string
		diskFullLines                 []string
		warnings                      []string
	)

	for _, line := range lines {
		if m := successDone.FindStringSubmatch(line); m != nil {
			successM = m
			continue
		}
		if diskFull.MatchString(line) && !matchesAnyNoise(line) {
			diskFullLines = append(diskFullLines, line)
			continue
		}
		if m := toplevelFail.FindStringSubmatch(line); m != nil {
			toplevelM = m
			continue
		}
		if m := packageFail.FindStringSubmatch(line); m != nil {
			if packageM == nil { // keep the first (root-cause) match
				packageM = m
			}
			continue
		}
		if m := warningRe.FindStringSubmatch(line); m != nil && len(warnings) < maxWarnings {
			warnings = append(warnings, group(warningRe, m, "message"))
		}
	}

	toplevelTarget := ""
	if toplevelM != nil {
		toplevelTarget = group(toplevelFail, toplevelM, "target")
	}

	switch {
	case successM != nil:
		return Summary{
			Classification: "success", Success: true,
			DurationSec: parseDuration(group(successDone, successM, "duration")),
			Evidence:    []string{successM[0]}, Warnings: warnings,
		}
	case len(diskFullLines) > 0:
		return Summary{
			Classification: "disk_full", Success: false, FailedTarget: toplevelTarget,
			Evidence: head(diskFullLines, maxEvidenceLine), Warnings: warnings,
		}
	case packageM != nil:
		step := group(packageFail, packageM, "step")
		return Summary{
			Classification: "failed_package", Success: false, FailedTarget: toplevelTarget,
			FailedStep: step, FailedPackage: extractFailedPackage(step),
			Evidence: []string{packageM[0]}, Warnings: warnings,
		}
	case toplevelM != nil:
		return Summary{
			Classification: "compile_error", Success: false, FailedTarget: toplevelTarget,
			Evidence: []string{toplevelM[0]}, Warnings: warnings,
		}
	default:
		return Summary{
			Classification: "unknown", Success: false,
			Evidence: nonBlankTail(lines, maxEvidenceLine), Warnings: warnings,
		}
	}
}

func extractFailedPackage(step string) string {
	if step == "" {
		return ""
	}
	if strings.HasSuffix(step, "world") || strings.Contains(step, "toplevel.mk") {
		return ""
	}
	parts := strings.Split(step, "/")
	if len(parts) < 2 {
		return ""
	}
	leaf := map[string]bool{"compile": true, "install": true, "configure": true}
	switch parts[0] {
	case "package":
		pp := parts[1:]
		if leaf[parts[len(parts)-1]] {
			pp = parts[1 : len(parts)-1]
		}
		if len(pp) == 0 {
			return ""
		}
		return strings.Join(pp, "/")
	case "target":
		pp := parts[1:]
		if leaf[parts[len(parts)-1]] {
			pp = parts[1 : len(parts)-1]
		}
		if len(pp) == 0 {
			return ""
		}
		return "target/" + strings.Join(pp, "/")
	}
	return ""
}

func parseDuration(value string) *float64 {
	parts := strings.Split(value, ":")
	switch len(parts) {
	case 2:
		m, err1 := strconv.Atoi(parts[0])
		s, err2 := strconv.ParseFloat(parts[1], 64)
		if err1 != nil || err2 != nil {
			return nil
		}
		return f(float64(m)*60 + s)
	case 3:
		h, err1 := strconv.Atoi(parts[0])
		m, err2 := strconv.Atoi(parts[1])
		s, err3 := strconv.ParseFloat(parts[2], 64)
		if err1 != nil || err2 != nil || err3 != nil {
			return nil
		}
		return f(float64(h)*3600 + float64(m)*60 + s)
	}
	return nil
}

func matchesAnyNoise(line string) bool {
	for _, re := range knownNoise {
		if re.MatchString(line) {
			return true
		}
	}
	return false
}

func group(re *regexp.Regexp, match []string, name string) string {
	idx := re.SubexpIndex(name)
	if idx < 0 || idx >= len(match) {
		return ""
	}
	return match[idx]
}

func head(s []string, n int) []string {
	if len(s) > n {
		return append([]string{}, s[:n]...)
	}
	return append([]string{}, s...)
}

func nonBlankTail(lines []string, n int) []string {
	start := 0
	if len(lines) > n {
		start = len(lines) - n
	}
	out := []string{}
	for _, line := range lines[start:] {
		if strings.TrimSpace(line) != "" {
			out = append(out, line)
		}
	}
	return out
}

func f(v float64) *float64 { return &v }

func floatOrNil(p *float64) any {
	if p == nil {
		return nil
	}
	return *p
}

func strOrNil(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func listOrEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}
