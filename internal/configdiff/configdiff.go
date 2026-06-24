// Package configdiff is the Go port of python/owrt_monitor/config_diff.py: it
// recursively diffs two redacted config snapshots into a path-sorted list of
// changes, used to surface what changed since the last successful run.
package configdiff

import (
	"fmt"
	"reflect"
	"sort"
)

// Missing is the placeholder shown when a key exists on only one side.
const Missing = "<missing>"

// Change is one difference between two snapshots. Path is dotted, e.g.
// "tests.smoke[2].command".
type Change struct {
	Path string `json:"path"`
	Old  any    `json:"old"`
	New  any    `json:"new"`
}

// Diff recursively diffs old vs new and returns changes sorted by path
// (config_diff.diff_configs).
func Diff(old, new any) []Change {
	changes := walk(old, new, "")
	sort.Slice(changes, func(i, j int) bool { return changes[i].Path < changes[j].Path })
	return changes
}

func walk(old, new any, prefix string) []Change {
	oldMap, oldIsMap := old.(map[string]any)
	newMap, newIsMap := new.(map[string]any)
	if oldIsMap && newIsMap {
		var out []Change
		for _, key := range unionKeys(oldMap, newMap) {
			subPath := key
			if prefix != "" {
				subPath = prefix + "." + key
			}
			subOld, hasOld := oldMap[key]
			subNew, hasNew := newMap[key]
			switch {
			case !hasOld:
				out = append(out, Change{subPath, Missing, subNew})
			case !hasNew:
				out = append(out, Change{subPath, subOld, Missing})
			default:
				out = append(out, walk(subOld, subNew, subPath)...)
			}
		}
		return out
	}

	oldList, oldIsList := old.([]any)
	newList, newIsList := new.([]any)
	if oldIsList && newIsList {
		if len(oldList) != len(newList) {
			return []Change{{rootOr(prefix), old, new}}
		}
		var out []Change
		for i := range oldList {
			out = append(out, walk(oldList[i], newList[i], fmt.Sprintf("%s[%d]", prefix, i))...)
		}
		return out
	}

	if !reflect.DeepEqual(old, new) {
		return []Change{{rootOr(prefix), old, new}}
	}
	return nil
}

// Summary is a compact view suitable for event payloads (config_diff.summarize).
type Summary struct {
	Total  int      `json:"total"`
	Sample []Change `json:"sample"`
}

// Summarize returns the total change count plus a bounded sample.
func Summarize(changes []Change, sampleLimit int) Summary {
	if sampleLimit < 0 {
		sampleLimit = 0
	}
	sample := changes
	if len(sample) > sampleLimit {
		sample = sample[:sampleLimit]
	}
	if sample == nil {
		sample = []Change{}
	}
	return Summary{Total: len(changes), Sample: sample}
}

func unionKeys(a, b map[string]any) []string {
	seen := map[string]bool{}
	var keys []string
	for k := range a {
		if !seen[k] {
			seen[k] = true
			keys = append(keys, k)
		}
	}
	for k := range b {
		if !seen[k] {
			seen[k] = true
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return keys
}

func rootOr(prefix string) string {
	if prefix == "" {
		return "<root>"
	}
	return prefix
}
