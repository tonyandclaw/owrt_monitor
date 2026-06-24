package artifact

import "testing"

func cands() []Candidate {
	return []Candidate{
		{Path: "a/small.bin", SizeBytes: 1 << 20, Mtime: 100},
		{Path: "b/big-old.bin", SizeBytes: 10 << 20, Mtime: 50},
		{Path: "c/new.bin", SizeBytes: 5 << 20, Mtime: 200},
	}
}

func TestSelectNewest(t *testing.T) {
	got, err := Select(cands(), "newest", 0, nil)
	if err != nil || got.Path != "c/new.bin" {
		t.Fatalf("newest = %v, %v", got, err)
	}
}

func TestSelectLargest(t *testing.T) {
	got, err := Select(cands(), "largest", 0, nil)
	if err != nil || got.Path != "b/big-old.bin" {
		t.Fatalf("largest = %v, %v", got, err)
	}
}

func TestSelectFailIfMultiple(t *testing.T) {
	if _, err := Select(cands(), "fail-if-multiple", 0, nil); err == nil {
		t.Error("fail-if-multiple with 3 candidates should error")
	}
	one := []Candidate{{Path: "only.bin", SizeBytes: 4 << 20, Mtime: 1}}
	got, err := Select(one, "fail-if-multiple", 0, nil)
	if err != nil || got.Path != "only.bin" {
		t.Fatalf("fail-if-multiple single = %v, %v", got, err)
	}
}

func TestSelectMinSizeFilter(t *testing.T) {
	// 8 MB floor eliminates all but big-old.bin.
	got, err := Select(cands(), "newest", 8, nil)
	if err != nil || got.Path != "b/big-old.bin" {
		t.Fatalf("min-size = %v, %v", got, err)
	}
}

func TestSelectRegexFilter(t *testing.T) {
	got, err := Select(cands(), "newest", 0, []string{`new\.bin$`})
	if err != nil || got.Path != "c/new.bin" {
		t.Fatalf("regex = %v, %v", got, err)
	}
	if _, err := Select(cands(), "newest", 0, []string{`nomatch`}); err == nil {
		t.Error("regex with no match should error")
	}
}

func TestSelectEmpty(t *testing.T) {
	if _, err := Select(nil, "newest", 0, nil); err == nil {
		t.Error("empty candidates should error")
	}
}

func TestFilename(t *testing.T) {
	if got := (Candidate{Path: "a/b/c.bin"}).Filename(); got != "c.bin" {
		t.Errorf("Filename = %q", got)
	}
}
