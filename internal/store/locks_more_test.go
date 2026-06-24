package store

import "testing"

func TestHeartbeatAndOwners(t *testing.T) {
	s, _ := openTestStore(t)

	if ok, err := s.AcquireDUTLock("dut-01", "jobA", nil); err != nil || !ok {
		t.Fatalf("acquire dut: %v %v", ok, err)
	}
	if err := s.HeartbeatDUTLock("dut-01", "jobA"); err != nil {
		t.Fatalf("heartbeat dut: %v", err)
	}
	if ok, err := s.AcquireBuilderLock("b1", "jobA", nil); err != nil || !ok {
		t.Fatalf("acquire builder: %v %v", ok, err)
	}
	if err := s.HeartbeatBuilderLock("b1", "jobA"); err != nil {
		t.Fatalf("heartbeat builder: %v", err)
	}
	if owner, held, _ := s.BuilderLockOwner("b1"); !held || owner != "jobA" {
		t.Errorf("builder owner = %q held=%v", owner, held)
	}

	// Owner lookups for missing locks report not-held.
	if _, held, _ := s.DUTLockOwner("nope"); held {
		t.Error("missing dut lock should not be held")
	}
	if _, held, _ := s.BuilderLockOwner("nope"); held {
		t.Error("missing builder lock should not be held")
	}
}

func TestReleaseLocksForJob(t *testing.T) {
	s, _ := openTestStore(t)
	_, _ = s.AcquireDUTLock("dut-01", "jobA", nil)
	_, _ = s.AcquireBuilderLock("b1", "jobA", nil)
	_, _ = s.AcquireDUTLock("dut-02", "other", nil)

	dn, bn, err := s.ReleaseLocksForJob("jobA")
	if err != nil {
		t.Fatalf("ReleaseLocksForJob: %v", err)
	}
	if dn != 1 || bn != 1 {
		t.Errorf("released dut=%d builder=%d, want 1/1", dn, bn)
	}
	// Other owner's lock is untouched.
	if owner, held, _ := s.DUTLockOwner("dut-02"); !held || owner != "other" {
		t.Errorf("dut-02 should remain held by other: %q %v", owner, held)
	}
	// Releasing again is a no-op (0/0).
	dn, bn, _ = s.ReleaseLocksForJob("jobA")
	if dn != 0 || bn != 0 {
		t.Errorf("second release = %d/%d, want 0/0", dn, bn)
	}
}

func TestReleaseRequiresOwner(t *testing.T) {
	s, _ := openTestStore(t)
	_, _ = s.AcquireDUTLock("dut-01", "jobA", nil)
	// Wrong owner release does not drop the lock.
	if err := s.ReleaseDUTLock("dut-01", "jobB"); err != nil {
		t.Fatalf("release: %v", err)
	}
	if _, held, _ := s.DUTLockOwner("dut-01"); !held {
		t.Error("lock should survive a non-owner release")
	}
}

func TestSetPIDAndRecentLimitDefault(t *testing.T) {
	s, _ := openTestStore(t)
	if err := s.CreateJob(Job{ID: "j1", ConfigPath: "c", ArtifactDir: "a", State: "PENDING", ConfigSnapshot: "{}"}); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := s.SetPID("j1", 1234); err != nil {
		t.Fatalf("SetPID: %v", err)
	}
	job, _ := s.GetJob("j1")
	if job.PID == nil || *job.PID != 1234 {
		t.Errorf("pid = %v, want 1234", job.PID)
	}
	// limit <= 0 falls back to the default (20) without error.
	if _, err := s.RecentJobs(0); err != nil {
		t.Errorf("RecentJobs(0): %v", err)
	}
}
