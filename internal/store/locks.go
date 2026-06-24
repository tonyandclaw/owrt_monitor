package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

// Lock semantics mirror Python's storage.py exactly so the two engines share
// one source of truth (the dut_locks / builder_locks tables) and one read model
// (<db_dir>/locks.json). serial_locks and artifact_locks are not modelled in
// SQLite; the snapshot preserves whatever a peer wrote there, matching
// storage.py._read_non_sql_locks.

// AcquireDUTLock takes the DUT lock for ownerJobID. A non-nil lockTimeoutSec
// lets a stale lock (heartbeat older than the timeout) be reclaimed. Returns
// false when the lock is currently held by someone else.
func (s *Store) AcquireDUTLock(dutName, ownerJobID string, lockTimeoutSec *int) (bool, error) {
	return s.acquireLock("dut_locks", "dut_name", dutName, ownerJobID, lockTimeoutSec)
}

// HeartbeatDUTLock refreshes the heartbeat for a held DUT lock.
func (s *Store) HeartbeatDUTLock(dutName, ownerJobID string) error {
	if _, err := s.db.Exec(
		"UPDATE dut_locks SET heartbeat_at = ? WHERE dut_name = ? AND owner_job_id = ?",
		NowISO(), dutName, ownerJobID,
	); err != nil {
		return err
	}
	return s.refreshLocksSnapshot()
}

// ReleaseDUTLock drops the DUT lock if owned by ownerJobID.
func (s *Store) ReleaseDUTLock(dutName, ownerJobID string) error {
	return s.releaseLock("dut_locks", "dut_name", dutName, ownerJobID)
}

// DUTLockOwner returns the current owner of a DUT lock, if any.
func (s *Store) DUTLockOwner(dutName string) (string, bool, error) {
	return s.lockOwner("dut_locks", "dut_name", dutName)
}

// AcquireBuilderLock takes the builder/container lock for ownerJobID.
func (s *Store) AcquireBuilderLock(builderName, ownerJobID string, lockTimeoutSec *int) (bool, error) {
	return s.acquireLock("builder_locks", "builder_name", builderName, ownerJobID, lockTimeoutSec)
}

// HeartbeatBuilderLock refreshes the heartbeat for a held builder lock.
func (s *Store) HeartbeatBuilderLock(builderName, ownerJobID string) error {
	if _, err := s.db.Exec(
		"UPDATE builder_locks SET heartbeat_at = ? WHERE builder_name = ? AND owner_job_id = ?",
		NowISO(), builderName, ownerJobID,
	); err != nil {
		return err
	}
	return s.refreshLocksSnapshot()
}

// ReleaseBuilderLock drops the builder lock if owned by ownerJobID.
func (s *Store) ReleaseBuilderLock(builderName, ownerJobID string) error {
	return s.releaseLock("builder_locks", "builder_name", builderName, ownerJobID)
}

// BuilderLockOwner returns the current owner of a builder lock, if any.
func (s *Store) BuilderLockOwner(builderName string) (string, bool, error) {
	return s.lockOwner("builder_locks", "builder_name", builderName)
}

// ReleaseLocksForJob drops every DUT and builder lock owned by a job. Used by
// orphan recovery after the recorded PID is known dead.
func (s *Store) ReleaseLocksForJob(ownerJobID string) (dutReleased, builderReleased int, err error) {
	dutRes, err := s.db.Exec("DELETE FROM dut_locks WHERE owner_job_id = ?", ownerJobID)
	if err != nil {
		return 0, 0, err
	}
	builderRes, err := s.db.Exec("DELETE FROM builder_locks WHERE owner_job_id = ?", ownerJobID)
	if err != nil {
		return 0, 0, err
	}
	dn, _ := dutRes.RowsAffected()
	bn, _ := builderRes.RowsAffected()
	if dn > 0 || bn > 0 {
		if err := s.refreshLocksSnapshot(); err != nil {
			return int(dn), int(bn), err
		}
	}
	return int(dn), int(bn), nil
}

func (s *Store) acquireLock(table, col, name, ownerJobID string, lockTimeoutSec *int) (bool, error) {
	tx, err := s.db.Begin()
	if err != nil {
		return false, err
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback()
		}
	}()

	var (
		existingOwner string
		heartbeat     string
	)
	err = tx.QueryRow(
		"SELECT owner_job_id, heartbeat_at FROM "+table+" WHERE "+col+" = ?", name,
	).Scan(&existingOwner, &heartbeat)
	switch {
	case err == nil:
		// Held: refuse unless the holder is stale and a timeout was given.
		if lockTimeoutSec == nil || !isStale(heartbeat, *lockTimeoutSec) {
			return false, nil // deferred rollback releases the tx
		}
		if _, err := tx.Exec(
			"DELETE FROM "+table+" WHERE "+col+" = ? AND owner_job_id = ?", name, existingOwner,
		); err != nil {
			return false, err
		}
	case errors.Is(err, sql.ErrNoRows):
		// free
	default:
		return false, err
	}

	now := NowISO()
	if _, err := tx.Exec(
		"INSERT INTO "+table+" ("+col+", owner_job_id, created_at, heartbeat_at) VALUES (?, ?, ?, ?)",
		name, ownerJobID, now, now,
	); err != nil {
		return false, err
	}
	if err := tx.Commit(); err != nil {
		return false, err
	}
	committed = true
	if err := s.refreshLocksSnapshot(); err != nil {
		return true, err
	}
	return true, nil
}

func (s *Store) releaseLock(table, col, name, ownerJobID string) error {
	if _, err := s.db.Exec(
		"DELETE FROM "+table+" WHERE "+col+" = ? AND owner_job_id = ?", name, ownerJobID,
	); err != nil {
		return err
	}
	return s.refreshLocksSnapshot()
}

func (s *Store) lockOwner(table, col, name string) (string, bool, error) {
	var owner string
	err := s.db.QueryRow(
		"SELECT owner_job_id FROM "+table+" WHERE "+col+" = ?", name,
	).Scan(&owner)
	if errors.Is(err, sql.ErrNoRows) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	return owner, true, nil
}

// refreshLocksSnapshot atomically rewrites <db_dir>/locks.json from the SQLite
// dut/builder tables, preserving any serial_locks / artifact_locks a peer
// wrote. Best-effort: SQLite stays the source of truth, so a snapshot failure
// never fails the caller's lock operation.
func (s *Store) refreshLocksSnapshot() error {
	duts, err := s.dumpLocks("dut_locks", "dut_name")
	if err != nil {
		return nil //nolint:nilerr // snapshot is best-effort, like storage.py
	}
	builders, err := s.dumpLocks("builder_locks", "builder_name")
	if err != nil {
		return nil //nolint:nilerr
	}
	snapshotPath := filepath.Join(filepath.Dir(s.path), "locks.json")
	extra := readNonSQLLocks(snapshotPath)
	payload := map[string]any{
		"generated_at":   NowISO(),
		"dut_locks":      duts,
		"builder_locks":  builders,
		"serial_locks":   extra["serial_locks"],
		"artifact_locks": extra["artifact_locks"],
	}
	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return nil //nolint:nilerr
	}
	data = append(data, '\n')
	tmp := snapshotPath + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return nil //nolint:nilerr
	}
	if err := os.Rename(tmp, snapshotPath); err != nil {
		_ = os.Remove(tmp)
		return nil //nolint:nilerr
	}
	return nil
}

// dumpLocks returns lock rows as ordered maps using the column name as the
// identity key (dut_name / builder_name), matching the Python snapshot shape.
func (s *Store) dumpLocks(table, col string) ([]map[string]any, error) {
	rows, err := s.db.Query(
		"SELECT " + col + ", owner_job_id, created_at, heartbeat_at FROM " + table + " ORDER BY " + col,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var name, owner, created, heartbeat string
		if err := rows.Scan(&name, &owner, &created, &heartbeat); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{
			col:            name,
			"owner_job_id": owner,
			"created_at":   created,
			"heartbeat_at": heartbeat,
		})
	}
	return out, rows.Err()
}

func readNonSQLLocks(snapshotPath string) map[string][]any {
	out := map[string][]any{"serial_locks": {}, "artifact_locks": {}}
	data, err := os.ReadFile(snapshotPath)
	if err != nil {
		return out
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		return out
	}
	for _, key := range []string{"serial_locks", "artifact_locks"} {
		if list, ok := payload[key].([]any); ok {
			out[key] = list
		}
	}
	return out
}
