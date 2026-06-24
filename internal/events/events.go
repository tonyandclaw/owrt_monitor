// Package events is the Go port of python/owrt_monitor/events.py. Each Emit
// appends one sorted-key JSON object to events.jsonl AND records the same event
// in the SQLite job_events table, so the daemon's events stream and either
// engine's reader see identical data.
package events

import (
	"encoding/json"
	"os"
	"path/filepath"

	"github.com/tonyandclaw/owrt_monitor/internal/store"
)

// Logger writes structured job events to both events.jsonl and SQLite.
type Logger struct {
	store *store.Store
	jobID string
	path  string
}

// New creates a Logger writing to path (events.jsonl), creating its directory.
func New(st *store.Store, jobID, path string) (*Logger, error) {
	if dir := filepath.Dir(path); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	return &Logger{store: st, jobID: jobID, path: path}, nil
}

// Emit appends one event to events.jsonl and records it in job_events. The
// JSONL object uses sorted keys (Go marshals map keys sorted), matching
// events.py's json.dumps(..., sort_keys=True).
func (l *Logger) Emit(level, component, event, message string, fields map[string]any) error {
	ts := store.NowISO()
	if fields == nil {
		fields = map[string]any{}
	}
	payload := map[string]any{
		"ts":        ts,
		"job_id":    l.jobID,
		"level":     level,
		"component": component,
		"event":     event,
		"message":   message,
		"fields":    fields,
	}
	line, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(l.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if l.store != nil {
		return l.store.RecordEvent(store.JobEvent{
			JobID: l.jobID, TS: ts, Level: level, Component: component,
			Event: event, Message: message, Fields: fields,
		})
	}
	return nil
}
