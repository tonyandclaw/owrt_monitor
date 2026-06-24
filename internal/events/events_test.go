package events

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/tonyandclaw/owrt_monitor/internal/store"
)

func TestEmitWritesJSONLAndDB(t *testing.T) {
	dir := t.TempDir()
	st, err := store.Open(filepath.Join(dir, "owrt_monitor.sqlite3"))
	if err != nil {
		t.Fatalf("store.Open: %v", err)
	}
	defer st.Close()
	if err := st.CreateJob(store.Job{ID: "j1", ConfigPath: "c", ArtifactDir: dir, State: "PENDING", ConfigSnapshot: "{}"}); err != nil {
		t.Fatalf("CreateJob: %v", err)
	}

	jsonl := filepath.Join(dir, "j1", "events.jsonl")
	log, err := New(st, "j1", jsonl)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if err := log.Emit("INFO", "builder", "build_started", "go", map[string]any{"container": "x"}); err != nil {
		t.Fatalf("Emit 1: %v", err)
	}
	if err := log.Emit("INFO", "builder", "build_succeeded", "done", nil); err != nil {
		t.Fatalf("Emit 2: %v", err)
	}

	// events.jsonl has two valid JSON lines with the expected keys.
	f, err := os.Open(jsonl)
	if err != nil {
		t.Fatalf("open jsonl: %v", err)
	}
	defer f.Close()
	count := 0
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var obj map[string]any
		if err := json.Unmarshal(sc.Bytes(), &obj); err != nil {
			t.Fatalf("line %d not JSON: %v", count, err)
		}
		for _, k := range []string{"ts", "job_id", "level", "component", "event", "message", "fields"} {
			if _, ok := obj[k]; !ok {
				t.Errorf("line %d missing key %q", count, k)
			}
		}
		count++
	}
	if count != 2 {
		t.Errorf("events.jsonl lines = %d, want 2", count)
	}

	// Same events landed in SQLite.
	dbEvents, err := st.EventsForJob("j1")
	if err != nil || len(dbEvents) != 2 {
		t.Fatalf("EventsForJob = %d, err %v", len(dbEvents), err)
	}
	if dbEvents[0].Fields["container"] != "x" {
		t.Errorf("fields not persisted to DB: %+v", dbEvents[0].Fields)
	}
}

// A nil store means jsonl-only logging (no DB side); Emit must still work.
func TestEmitNilStoreJSONLOnly(t *testing.T) {
	dir := t.TempDir()
	jsonl := filepath.Join(dir, "events.jsonl")
	log, err := New(nil, "j1", jsonl)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if err := log.Emit("INFO", "wf", "evt", "msg", nil); err != nil {
		t.Fatalf("Emit: %v", err)
	}
	data, err := os.ReadFile(jsonl)
	if err != nil {
		t.Fatalf("read jsonl: %v", err)
	}
	var obj map[string]any
	if err := json.Unmarshal([]byte(splitFirstLine(string(data))), &obj); err != nil {
		t.Fatalf("jsonl line invalid: %v", err)
	}
	if obj["event"] != "evt" {
		t.Errorf("event = %v", obj["event"])
	}
	// Empty fields default to an object, not null.
	if _, ok := obj["fields"].(map[string]any); !ok {
		t.Errorf("fields should be an object, got %T", obj["fields"])
	}
}

func splitFirstLine(s string) string {
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			return s[:i]
		}
	}
	return s
}
