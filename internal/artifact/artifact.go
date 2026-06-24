// Package artifact is the Go port of python/owrt_monitor/artifacts.py: the
// firmware artifact candidate model, the exported-artifact record, and the
// selection policy (newest / largest / fail-if-multiple) with size and regex
// filtering.
package artifact

import (
	"fmt"
	"path"
	"regexp"
)

// SelectionError is raised when selection cannot produce one safe candidate
// (artifacts.ArtifactSelectionError).
type SelectionError struct{ msg string }

func (e *SelectionError) Error() string { return e.msg }

func selErr(format string, args ...any) *SelectionError {
	return &SelectionError{msg: fmt.Sprintf(format, args...)}
}

// Candidate is one firmware file discovered in the build output
// (artifacts.ArtifactCandidate).
type Candidate struct {
	Path      string // path relative to the builder workdir
	SizeBytes int64
	Mtime     float64 // epoch seconds
}

// Filename returns the base name of the candidate path.
func (c Candidate) Filename() string { return path.Base(c.Path) }

// ExportedArtifact records a firmware file copied to the host
// (artifacts.ExportedArtifact). Shared by the report and workflow layers.
type ExportedArtifact struct {
	ContainerPath string
	HostPath      string
	Filename      string
	SizeBytes     int64
	SHA256        string
}

// Select chooses one candidate per the configured policy, after applying the
// size floor and optional regex filters (artifacts.select_artifact).
func Select(candidates []Candidate, selection string, minSizeMB float64, regexPatterns []string) (Candidate, error) {
	if len(candidates) == 0 {
		return Candidate{}, selErr("no artifacts matched the configured artifact patterns")
	}

	minBytes := int64(minSizeMB * 1024 * 1024)
	eligible := make([]Candidate, 0, len(candidates))
	for _, c := range candidates {
		if c.SizeBytes >= minBytes {
			eligible = append(eligible, c)
		}
	}

	if len(regexPatterns) > 0 {
		compiled := make([]*regexp.Regexp, 0, len(regexPatterns))
		for _, p := range regexPatterns {
			re, err := regexp.Compile(p)
			if err != nil {
				return Candidate{}, selErr("invalid regex_pattern %q: %v", p, err)
			}
			compiled = append(compiled, re)
		}
		filtered := eligible[:0:0]
		for _, c := range eligible {
			if matchesAll(c.Path, compiled) {
				filtered = append(filtered, c)
			}
		}
		eligible = filtered
		if len(eligible) == 0 {
			return Candidate{}, selErr("no firmware artifacts matched all regex_patterns: %v", regexPatterns)
		}
	}

	if len(eligible) == 0 {
		return Candidate{}, selErr("no firmware artifacts matched the size threshold (%g MB)", minSizeMB)
	}

	switch selection {
	case "newest":
		return pick(eligible, func(a, b Candidate) bool {
			if a.Mtime != b.Mtime {
				return a.Mtime > b.Mtime
			}
			return a.SizeBytes > b.SizeBytes
		}), nil
	case "largest":
		return pick(eligible, func(a, b Candidate) bool {
			if a.SizeBytes != b.SizeBytes {
				return a.SizeBytes > b.SizeBytes
			}
			return a.Mtime > b.Mtime
		}), nil
	case "fail-if-multiple":
		if len(eligible) != 1 {
			paths := ""
			for i, c := range eligible {
				if i > 0 {
					paths += ", "
				}
				paths += c.Path
			}
			return Candidate{}, selErr("expected one artifact, found %d: %s", len(eligible), paths)
		}
		return eligible[0], nil
	default:
		return Candidate{}, selErr("unsupported artifact selection policy: %s", selection)
	}
}

func matchesAll(s string, res []*regexp.Regexp) bool {
	for _, re := range res {
		if !re.MatchString(s) {
			return false
		}
	}
	return true
}

// pick returns the element that is "best" under better(best, candidate).
func pick(items []Candidate, better func(best, c Candidate) bool) Candidate {
	best := items[0]
	for _, c := range items[1:] {
		if better(c, best) {
			best = c
		}
	}
	return best
}
