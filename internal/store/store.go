// Package store is the pure-Go SQLite persistence layer for the standalone Go
// engine. It is wire-compatible with the Python engine's storage.py: identical
// database file, table schema, TEXT timestamp format, and locks.json snapshot.
// Either stack can open and read the other's <artifact_root>/owrt_monitor.sqlite3
// and the companion locks.json, so the two engines interoperate over the same
// artifact directory.
//
// The driver is modernc.org/sqlite (pure Go, no cgo) so the engine cross-compiles
// without a C toolchain — matching this repo's dep-free-Go preference.
package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite"
)

// isoLayout matches Python's datetime.now(UTC).isoformat(): microsecond
// precision with a numeric UTC offset (e.g. 2026-06-24T07:12:34.567890+00:00).
const isoLayout = "2006-01-02T15:04:05.000000-07:00"

// NowISO returns a UTC timestamp string compatible with Python's isoformat().
func NowISO() string { return time.Now().UTC().Format(isoLayout) }

// ParseTimestamp accepts the timestamp shapes either engine may have written.
func ParseTimestamp(raw string) (time.Time, error) {
	layouts := []string{
		isoLayout,
		time.RFC3339Nano,
		time.RFC3339,
		"2006-01-02T15:04:05",
	}
	var lastErr error
	for _, layout := range layouts {
		if t, err := time.Parse(layout, raw); err == nil {
			return t.UTC(), nil
		} else {
			lastErr = err
		}
	}
	return time.Time{}, lastErr
}

// Store wraps the shared SQLite database.
type Store struct {
	db   *sql.DB
	path string
}

// Job mirrors a row of the jobs table.
type Job struct {
	ID             string
	ConfigPath     string
	ArtifactDir    string
	State          string
	Result         string // empty == NULL
	StartedAt      string
	FinishedAt     string // empty == NULL
	ConfigSnapshot string
	PID            *int   // nil == NULL
	Metrics        string // empty == NULL (JSON text)
}

// JobEvent mirrors a row of the job_events table.
type JobEvent struct {
	JobID     string
	TS        string
	Level     string
	Component string
	Event     string
	Message   string
	Fields    map[string]any
}

// Artifact mirrors a row of the artifacts table.
type Artifact struct {
	JobID         string
	ContainerPath string
	HostPath      string
	Filename      string
	SizeBytes     int64
	SHA256        string
	CreatedAt     string
}

// TestResult mirrors a row of the test_results table.
type TestResult struct {
	JobID       string
	Command     string
	Passed      bool
	Output      string
	DurationSec float64
	CreatedAt   string
}

// Open opens (creating if needed) the shared SQLite database and ensures the
// schema is present and migrated to the current shape.
func Open(path string) (*Store, error) {
	if dir := filepath.Dir(path); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("create db dir: %w", err)
		}
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// Match Python's reliance on SQLite waiting out a busy writer rather than
	// erroring immediately, so the two engines can share the file.
	if _, err := db.Exec("PRAGMA busy_timeout=5000"); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("set busy_timeout: %w", err)
	}
	s := &Store{db: db, path: path}
	if err := s.initSchema(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) initSchema() error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS jobs (
		  id TEXT PRIMARY KEY,
		  config_path TEXT NOT NULL,
		  artifact_dir TEXT NOT NULL,
		  state TEXT NOT NULL,
		  result TEXT,
		  started_at TEXT NOT NULL,
		  finished_at TEXT,
		  config_snapshot TEXT NOT NULL,
		  pid INTEGER,
		  metrics TEXT
		)`,
		`CREATE TABLE IF NOT EXISTS job_events (
		  id INTEGER PRIMARY KEY AUTOINCREMENT,
		  job_id TEXT NOT NULL,
		  ts TEXT NOT NULL,
		  level TEXT NOT NULL,
		  component TEXT NOT NULL,
		  event TEXT NOT NULL,
		  message TEXT NOT NULL,
		  fields TEXT NOT NULL,
		  FOREIGN KEY(job_id) REFERENCES jobs(id)
		)`,
		`CREATE TABLE IF NOT EXISTS artifacts (
		  id INTEGER PRIMARY KEY AUTOINCREMENT,
		  job_id TEXT NOT NULL,
		  container_path TEXT NOT NULL,
		  host_path TEXT NOT NULL,
		  filename TEXT NOT NULL,
		  size_bytes INTEGER NOT NULL,
		  sha256 TEXT NOT NULL,
		  created_at TEXT NOT NULL,
		  FOREIGN KEY(job_id) REFERENCES jobs(id)
		)`,
		`CREATE TABLE IF NOT EXISTS dut_locks (
		  dut_name TEXT PRIMARY KEY,
		  owner_job_id TEXT NOT NULL,
		  created_at TEXT NOT NULL,
		  heartbeat_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS builder_locks (
		  builder_name TEXT PRIMARY KEY,
		  owner_job_id TEXT NOT NULL,
		  created_at TEXT NOT NULL,
		  heartbeat_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS test_results (
		  id INTEGER PRIMARY KEY AUTOINCREMENT,
		  job_id TEXT NOT NULL,
		  command TEXT NOT NULL,
		  passed INTEGER NOT NULL,
		  output TEXT NOT NULL,
		  duration_sec REAL NOT NULL,
		  created_at TEXT NOT NULL,
		  FOREIGN KEY(job_id) REFERENCES jobs(id)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id, ts)`,
		`CREATE INDEX IF NOT EXISTS idx_test_results_job_id ON test_results(job_id, created_at)`,
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	for _, stmt := range statements {
		if _, err := tx.Exec(stmt); err != nil {
			return fmt.Errorf("init schema: %w", err)
		}
	}
	if err := migrateJobs(tx); err != nil {
		return err
	}
	return tx.Commit()
}

// migrateJobs mirrors storage.py._migrate: tolerate older Python DBs that
// predate the pid/metrics columns.
func migrateJobs(tx *sql.Tx) error {
	rows, err := tx.Query("PRAGMA table_info(jobs)")
	if err != nil {
		return err
	}
	defer rows.Close()
	existing := map[string]bool{}
	for rows.Next() {
		var (
			cid     int
			name    string
			ctype   string
			notnull int
			dflt    sql.NullString
			pk      int
		)
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			return err
		}
		existing[name] = true
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if !existing["pid"] {
		if _, err := tx.Exec("ALTER TABLE jobs ADD COLUMN pid INTEGER"); err != nil {
			return err
		}
	}
	if !existing["metrics"] {
		if _, err := tx.Exec("ALTER TABLE jobs ADD COLUMN metrics TEXT"); err != nil {
			return err
		}
	}
	return nil
}

// --- jobs ---------------------------------------------------------------

// CreateJob inserts a new job row. StartedAt defaults to now when empty.
func (s *Store) CreateJob(job Job) error {
	if job.StartedAt == "" {
		job.StartedAt = NowISO()
	}
	_, err := s.db.Exec(
		`INSERT INTO jobs (id, config_path, artifact_dir, state, result,
		   started_at, finished_at, config_snapshot, pid, metrics)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		job.ID, job.ConfigPath, job.ArtifactDir, job.State,
		nullString(job.Result), job.StartedAt, nullString(job.FinishedAt),
		job.ConfigSnapshot, nullIntPtr(job.PID), nullString(job.Metrics),
	)
	return err
}

// SetState updates only the state column.
func (s *Store) SetState(jobID, state string) error {
	_, err := s.db.Exec("UPDATE jobs SET state = ? WHERE id = ?", state, jobID)
	return err
}

// FinishJob records the terminal state, result, and finished_at timestamp.
func (s *Store) FinishJob(jobID, state, result string) error {
	_, err := s.db.Exec(
		"UPDATE jobs SET state = ?, result = ?, finished_at = ? WHERE id = ?",
		state, nullString(result), NowISO(), jobID,
	)
	return err
}

// SetMetrics stores the metrics JSON blob for a job.
func (s *Store) SetMetrics(jobID, metricsJSON string) error {
	_, err := s.db.Exec("UPDATE jobs SET metrics = ? WHERE id = ?", nullString(metricsJSON), jobID)
	return err
}

// SetPID records the supervised child PID for a job.
func (s *Store) SetPID(jobID string, pid int) error {
	_, err := s.db.Exec("UPDATE jobs SET pid = ? WHERE id = ?", pid, jobID)
	return err
}

// GetJob returns a single job, or (nil, nil) when not found.
func (s *Store) GetJob(jobID string) (*Job, error) {
	row := s.db.QueryRow(
		`SELECT id, config_path, artifact_dir, state, result, started_at,
		   finished_at, config_snapshot, pid, metrics FROM jobs WHERE id = ?`,
		jobID,
	)
	job, err := scanJob(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return job, nil
}

// RecentJobs returns up to limit jobs, newest started_at first.
func (s *Store) RecentJobs(limit int) ([]Job, error) {
	if limit <= 0 {
		limit = 20
	}
	rows, err := s.db.Query(
		`SELECT id, config_path, artifact_dir, state, result, started_at,
		   finished_at, config_snapshot, pid, metrics
		 FROM jobs ORDER BY started_at DESC LIMIT ?`,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Job
	for rows.Next() {
		job, err := scanJob(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *job)
	}
	return out, rows.Err()
}

type scanner interface {
	Scan(dest ...any) error
}

func scanJob(row scanner) (*Job, error) {
	var (
		job        Job
		result     sql.NullString
		finishedAt sql.NullString
		pid        sql.NullInt64
		metrics    sql.NullString
	)
	if err := row.Scan(
		&job.ID, &job.ConfigPath, &job.ArtifactDir, &job.State, &result,
		&job.StartedAt, &finishedAt, &job.ConfigSnapshot, &pid, &metrics,
	); err != nil {
		return nil, err
	}
	job.Result = result.String
	job.FinishedAt = finishedAt.String
	job.Metrics = metrics.String
	if pid.Valid {
		v := int(pid.Int64)
		job.PID = &v
	}
	return &job, nil
}

// --- events -------------------------------------------------------------

// RecordEvent appends a row to job_events (the SQLite side of EventLogger).
func (s *Store) RecordEvent(evt JobEvent) error {
	if evt.TS == "" {
		evt.TS = NowISO()
	}
	fields := evt.Fields
	if fields == nil {
		fields = map[string]any{}
	}
	data, err := json.Marshal(fields)
	if err != nil {
		return err
	}
	_, err = s.db.Exec(
		`INSERT INTO job_events (job_id, ts, level, component, event, message, fields)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		evt.JobID, evt.TS, evt.Level, evt.Component, evt.Event, evt.Message, string(data),
	)
	return err
}

// EventsForJob returns events for a job in (ts, id) order.
func (s *Store) EventsForJob(jobID string) ([]JobEvent, error) {
	rows, err := s.db.Query(
		`SELECT job_id, ts, level, component, event, message, fields
		 FROM job_events WHERE job_id = ? ORDER BY ts, id`,
		jobID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []JobEvent
	for rows.Next() {
		var (
			evt    JobEvent
			fields string
		)
		if err := rows.Scan(&evt.JobID, &evt.TS, &evt.Level, &evt.Component,
			&evt.Event, &evt.Message, &fields); err != nil {
			return nil, err
		}
		evt.Fields = map[string]any{}
		if fields != "" {
			_ = json.Unmarshal([]byte(fields), &evt.Fields)
		}
		out = append(out, evt)
	}
	return out, rows.Err()
}

// --- artifacts ----------------------------------------------------------

// RecordArtifact inserts an artifact row.
func (s *Store) RecordArtifact(a Artifact) error {
	if a.CreatedAt == "" {
		a.CreatedAt = NowISO()
	}
	_, err := s.db.Exec(
		`INSERT INTO artifacts (job_id, container_path, host_path, filename,
		   size_bytes, sha256, created_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		a.JobID, a.ContainerPath, a.HostPath, a.Filename, a.SizeBytes, a.SHA256, a.CreatedAt,
	)
	return err
}

// --- test results -------------------------------------------------------

// RecordTestResult inserts a test_results row.
func (s *Store) RecordTestResult(r TestResult) error {
	if r.CreatedAt == "" {
		r.CreatedAt = NowISO()
	}
	passed := 0
	if r.Passed {
		passed = 1
	}
	_, err := s.db.Exec(
		`INSERT INTO test_results (job_id, command, passed, output, duration_sec, created_at)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		r.JobID, r.Command, passed, r.Output, r.DurationSec, r.CreatedAt,
	)
	return err
}

// --- helpers ------------------------------------------------------------

func nullString(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func nullIntPtr(p *int) any {
	if p == nil {
		return nil
	}
	return *p
}

func isStale(heartbeat string, timeoutSec int) bool {
	t, err := ParseTimestamp(heartbeat)
	if err != nil {
		return true
	}
	return t.Before(time.Now().UTC().Add(-time.Duration(timeoutSec) * time.Second))
}
