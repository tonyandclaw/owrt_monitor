package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func openTestStore(t *testing.T) (*Store, string) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "owrt_monitor.sqlite3")
	s, err := Open(dbPath)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s, dir
}

func TestNowISOFormat(t *testing.T) {
	got := NowISO()
	// Round-trips through the same layout and ends with a numeric UTC offset,
	// matching Python's datetime.now(UTC).isoformat().
	if _, err := ParseTimestamp(got); err != nil {
		t.Fatalf("NowISO %q not parseable: %v", got, err)
	}
	if got[len(got)-6:] != "+00:00" {
		t.Errorf("NowISO %q should end with +00:00", got)
	}
}

func TestParseTimestampAcceptsPeerFormats(t *testing.T) {
	cases := []string{
		"2026-06-24T07:12:34.567890+00:00", // Python isoformat
		"2026-06-24T07:12:34.567890123Z",   // Go RFC3339Nano (daemon)
		"2026-06-24T07:12:34Z",             // RFC3339 no fraction
		"2026-06-24T07:12:34",              // naive
	}
	for _, c := range cases {
		if _, err := ParseTimestamp(c); err != nil {
			t.Errorf("ParseTimestamp(%q): %v", c, err)
		}
	}
}

func TestJobLifecycle(t *testing.T) {
	s, _ := openTestStore(t)
	pid := 4242
	job := Job{
		ID:             "job_abc123def456",
		ConfigPath:     "configs/example.yaml",
		ArtifactDir:    "/art",
		State:          "PENDING",
		ConfigSnapshot: `{"project":{"name":"lab"}}`,
		PID:            &pid,
	}
	if err := s.CreateJob(job); err != nil {
		t.Fatalf("CreateJob: %v", err)
	}

	got, err := s.GetJob(job.ID)
	if err != nil || got == nil {
		t.Fatalf("GetJob: %v (got=%v)", err, got)
	}
	if got.State != "PENDING" || got.PID == nil || *got.PID != pid {
		t.Errorf("unexpected job after create: %+v", got)
	}
	if got.StartedAt == "" {
		t.Error("StartedAt should be auto-populated")
	}

	if err := s.SetState(job.ID, "BUILD_RUNNING"); err != nil {
		t.Fatalf("SetState: %v", err)
	}
	if err := s.SetMetrics(job.ID, `{"total_duration_sec":12.5}`); err != nil {
		t.Fatalf("SetMetrics: %v", err)
	}
	if err := s.FinishJob(job.ID, "SUCCEEDED", "success"); err != nil {
		t.Fatalf("FinishJob: %v", err)
	}

	got, err = s.GetJob(job.ID)
	if err != nil {
		t.Fatalf("GetJob after finish: %v", err)
	}
	if got.State != "SUCCEEDED" || got.Result != "success" || got.FinishedAt == "" {
		t.Errorf("unexpected finished job: %+v", got)
	}
	if got.Metrics == "" {
		t.Error("metrics not persisted")
	}

	// missing job → (nil, nil)
	missing, err := s.GetJob("nope")
	if err != nil || missing != nil {
		t.Errorf("GetJob(missing) = (%v, %v), want (nil, nil)", missing, err)
	}

	jobs, err := s.RecentJobs(10)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("RecentJobs = %d jobs, err %v", len(jobs), err)
	}
}

func TestEventsArtifactsTestResults(t *testing.T) {
	s, _ := openTestStore(t)
	if err := s.CreateJob(Job{ID: "j1", ConfigPath: "c", ArtifactDir: "a", State: "PENDING", ConfigSnapshot: "{}"}); err != nil {
		t.Fatalf("CreateJob: %v", err)
	}

	if err := s.RecordEvent(JobEvent{
		JobID: "j1", Level: "INFO", Component: "builder", Event: "build_started",
		Message: "go", Fields: map[string]any{"container": "openwrt-builder"},
	}); err != nil {
		t.Fatalf("RecordEvent: %v", err)
	}
	events, err := s.EventsForJob("j1")
	if err != nil || len(events) != 1 {
		t.Fatalf("EventsForJob = %d, err %v", len(events), err)
	}
	if events[0].Fields["container"] != "openwrt-builder" {
		t.Errorf("event fields not round-tripped: %+v", events[0].Fields)
	}

	if err := s.RecordArtifact(Artifact{
		JobID: "j1", ContainerPath: "/c/fw.bin", HostPath: "/h/fw.bin",
		Filename: "fw.bin", SizeBytes: 4 << 20, SHA256: "deadbeef",
	}); err != nil {
		t.Fatalf("RecordArtifact: %v", err)
	}

	if err := s.RecordTestResult(TestResult{
		JobID: "j1", Command: "ubus call system board", Passed: true,
		Output: "ok", DurationSec: 0.42,
	}); err != nil {
		t.Fatalf("RecordTestResult: %v", err)
	}
}

func TestDUTLockExclusionAndRelease(t *testing.T) {
	s, dir := openTestStore(t)

	ok, err := s.AcquireDUTLock("dut-01", "jobA", nil)
	if err != nil || !ok {
		t.Fatalf("first acquire = %v, %v; want true", ok, err)
	}

	// Second owner is refused while held without a timeout.
	ok, err = s.AcquireDUTLock("dut-01", "jobB", nil)
	if err != nil {
		t.Fatalf("contended acquire err: %v", err)
	}
	if ok {
		t.Fatal("second acquire should be refused while lock is held")
	}

	owner, held, err := s.DUTLockOwner("dut-01")
	if err != nil || !held || owner != "jobA" {
		t.Fatalf("DUTLockOwner = (%q, %v, %v), want jobA held", owner, held, err)
	}

	// Snapshot must reflect the held lock and exist next to the db.
	snap := readSnapshot(t, dir)
	dutLocks, _ := snap["dut_locks"].([]any)
	if len(dutLocks) != 1 {
		t.Fatalf("snapshot dut_locks = %v, want 1 entry", snap["dut_locks"])
	}

	if err := s.ReleaseDUTLock("dut-01", "jobA"); err != nil {
		t.Fatalf("release: %v", err)
	}
	_, held, _ = s.DUTLockOwner("dut-01")
	if held {
		t.Fatal("lock should be released")
	}

	// Now jobB can take it.
	ok, err = s.AcquireDUTLock("dut-01", "jobB", nil)
	if err != nil || !ok {
		t.Fatalf("acquire after release = %v, %v; want true", ok, err)
	}
}

func TestStaleLockReclaim(t *testing.T) {
	s, _ := openTestStore(t)
	if ok, err := s.AcquireBuilderLock("openwrt-builder", "old", nil); err != nil || !ok {
		t.Fatalf("seed acquire: %v %v", ok, err)
	}
	// Backdate the heartbeat far into the past so it is stale.
	if _, err := s.db.Exec(
		"UPDATE builder_locks SET heartbeat_at = ? WHERE builder_name = ?",
		"2000-01-01T00:00:00.000000+00:00", "openwrt-builder",
	); err != nil {
		t.Fatalf("backdate: %v", err)
	}
	timeout := 60
	ok, err := s.AcquireBuilderLock("openwrt-builder", "new", &timeout)
	if err != nil || !ok {
		t.Fatalf("stale reclaim = %v, %v; want true", ok, err)
	}
	owner, _, _ := s.BuilderLockOwner("openwrt-builder")
	if owner != "new" {
		t.Errorf("owner after reclaim = %q, want new", owner)
	}
}

func TestSnapshotPreservesPeerSerialArtifactLocks(t *testing.T) {
	s, dir := openTestStore(t)
	snapPath := filepath.Join(dir, "locks.json")

	// Simulate a peer (Go daemon) having written serial/artifact locks.
	peer := map[string]any{
		"generated_at":  "2026-06-24T00:00:00.000000+00:00",
		"dut_locks":     []any{},
		"builder_locks": []any{},
		"serial_locks": []any{
			map[string]any{"name": "/dev/ttyUSB0", "owner_job_id": "peer", "created_at": "x", "heartbeat_at": "x"},
		},
		"artifact_locks": []any{},
	}
	writeJSON(t, snapPath, peer)

	// A Python-style mutation (here via Go) must not clobber serial_locks.
	if ok, err := s.AcquireDUTLock("dut-01", "jobA", nil); err != nil || !ok {
		t.Fatalf("acquire: %v %v", ok, err)
	}
	snap := readSnapshot(t, dir)
	serial, _ := snap["serial_locks"].([]any)
	if len(serial) != 1 {
		t.Fatalf("serial_locks clobbered: %v", snap["serial_locks"])
	}
}

func readSnapshot(t *testing.T, dir string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, "locks.json"))
	if err != nil {
		t.Fatalf("read snapshot: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("snapshot not valid JSON: %v", err)
	}
	return out
}

func writeJSON(t *testing.T, path string, v any) {
	t.Helper()
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(data, '\n'), 0o644); err != nil {
		t.Fatal(err)
	}
}
