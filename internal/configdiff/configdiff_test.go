package configdiff

import "testing"

func TestNoChange(t *testing.T) {
	a := map[string]any{"x": 1, "y": []any{"a", "b"}}
	b := map[string]any{"x": 1, "y": []any{"a", "b"}}
	if got := Diff(a, b); len(got) != 0 {
		t.Errorf("expected no changes, got %v", got)
	}
}

func TestScalarChange(t *testing.T) {
	got := Diff(map[string]any{"x": 1}, map[string]any{"x": 2})
	if len(got) != 1 || got[0].Path != "x" {
		t.Fatalf("got %v", got)
	}
}

func TestAddedAndRemoved(t *testing.T) {
	got := Diff(map[string]any{"a": 1}, map[string]any{"b": 2})
	// sorted by path: a (removed), b (added)
	if len(got) != 2 || got[0].Path != "a" || got[0].New != Missing || got[1].Path != "b" || got[1].Old != Missing {
		t.Fatalf("got %v", got)
	}
}

func TestListLengthDiffersIsWholeChange(t *testing.T) {
	got := Diff(map[string]any{"l": []any{1, 2}}, map[string]any{"l": []any{1, 2, 3}})
	if len(got) != 1 || got[0].Path != "l" {
		t.Fatalf("got %v", got)
	}
}

func TestListElementwise(t *testing.T) {
	got := Diff(map[string]any{"l": []any{"a", "b"}}, map[string]any{"l": []any{"a", "z"}})
	if len(got) != 1 || got[0].Path != "l[1]" {
		t.Fatalf("got %v", got)
	}
}

func TestNestedDotted(t *testing.T) {
	a := map[string]any{"builder": map[string]any{"container": "old"}}
	b := map[string]any{"builder": map[string]any{"container": "new"}}
	got := Diff(a, b)
	if len(got) != 1 || got[0].Path != "builder.container" {
		t.Fatalf("got %v", got)
	}
}

func TestSummarize(t *testing.T) {
	changes := []Change{{Path: "a"}, {Path: "b"}, {Path: "c"}}
	s := Summarize(changes, 2)
	if s.Total != 3 || len(s.Sample) != 2 {
		t.Errorf("summary = %+v", s)
	}
	empty := Summarize(nil, 5)
	if empty.Total != 0 || empty.Sample == nil {
		t.Errorf("empty summary should have non-nil sample: %+v", empty)
	}
}
