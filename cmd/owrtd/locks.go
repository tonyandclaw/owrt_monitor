package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

func (s *server) handleLocks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
		return
	}
	snapshot, err := s.readLocksSnapshot()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, snapshot)
}

func (s *server) handleLockByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "POST only"})
		return
	}
	rest := strings.TrimPrefix(r.URL.Path, "/v1/locks/")
	parts := strings.Split(rest, "/")
	if len(parts) != 3 {
		writeJSON(w, http.StatusNotFound, errorResponse{
			Error: "expected /v1/locks/{dut|builder|serial|artifact}/{name}/{acquire|heartbeat|release}",
		})
		return
	}
	kind := normalizeLockKind(parts[0])
	name := parts[1]
	action := parts[2]
	if kind == "" {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error: "lock kind must be dut, builder, container, serial, or artifact",
		})
		return
	}
	if !isSafeLockName(name) {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error: "lock name must be a non-empty path segment without control characters",
		})
		return
	}
	var req lockMutationRequest
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: fmt.Sprintf("invalid JSON: %v", err)})
		return
	}
	req.OwnerJobID = strings.TrimSpace(req.OwnerJobID)
	if !isSafeJobID(req.OwnerJobID) {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error: "owner_job_id must contain only alphanumerics, underscore, or hyphen",
		})
		return
	}
	var (
		payload map[string]any
		status  int
		err     error
	)
	switch action {
	case "acquire":
		payload, status, err = s.acquireResourceLock(
			kind, name, req.OwnerJobID, req.LockTimeoutSec,
		)
	case "heartbeat":
		payload, status, err = s.heartbeatResourceLock(kind, name, req.OwnerJobID)
	case "release":
		payload, status, err = s.releaseResourceLock(kind, name, req.OwnerJobID)
	default:
		writeJSON(w, http.StatusNotFound, errorResponse{
			Error: "lock action must be acquire, heartbeat, or release",
		})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	writeJSON(w, status, payload)
}

func normalizeLockKind(raw string) string {
	switch raw {
	case "dut":
		return "dut"
	case "builder", "container":
		return "builder"
	case "serial":
		return "serial"
	case "artifact":
		return "artifact"
	default:
		return ""
	}
}

func (s *server) acquireResourceLock(
	kind string,
	name string,
	ownerJobID string,
	timeoutSec int,
) (map[string]any, int, error) {
	s.locksMu.Lock()
	defer s.locksMu.Unlock()

	snapshot, err := s.readLocksSnapshotUnlocked()
	if err != nil {
		return nil, 0, err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	switch kind {
	case "dut":
		if idx := findDutLock(snapshot.DutLocks, name); idx >= 0 {
			existing := snapshot.DutLocks[idx]
			if timeoutSec <= 0 || !lockHeartbeatIsStale(existing.HeartbeatAt, timeoutSec) {
				return lockHeldResponse(kind, name, existing.OwnerJobID), http.StatusConflict, nil
			}
			snapshot.DutLocks = append(snapshot.DutLocks[:idx], snapshot.DutLocks[idx+1:]...)
		}
		lock := dutLock{
			DutName:     name,
			OwnerJobID:  ownerJobID,
			CreatedAt:   now,
			HeartbeatAt: now,
		}
		snapshot.DutLocks = append(snapshot.DutLocks, lock)
		sortLocks(&snapshot)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"acquired":     true,
			"lock":         lock,
		}, http.StatusCreated, nil
	case "builder":
		if idx := findBuilderLock(snapshot.BuilderLocks, name); idx >= 0 {
			existing := snapshot.BuilderLocks[idx]
			if timeoutSec <= 0 || !lockHeartbeatIsStale(existing.HeartbeatAt, timeoutSec) {
				return lockHeldResponse(kind, name, existing.OwnerJobID), http.StatusConflict, nil
			}
			snapshot.BuilderLocks = append(
				snapshot.BuilderLocks[:idx],
				snapshot.BuilderLocks[idx+1:]...,
			)
		}
		lock := builderLock{
			BuilderName: name,
			OwnerJobID:  ownerJobID,
			CreatedAt:   now,
			HeartbeatAt: now,
		}
		snapshot.BuilderLocks = append(snapshot.BuilderLocks, lock)
		sortLocks(&snapshot)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"acquired":     true,
			"lock":         lock,
		}, http.StatusCreated, nil
	case "serial":
		if idx := findNamedLock(snapshot.SerialLocks, name); idx >= 0 {
			existing := snapshot.SerialLocks[idx]
			if timeoutSec <= 0 || !lockHeartbeatIsStale(existing.HeartbeatAt, timeoutSec) {
				return lockHeldResponse(kind, name, existing.OwnerJobID), http.StatusConflict, nil
			}
			snapshot.SerialLocks = append(
				snapshot.SerialLocks[:idx],
				snapshot.SerialLocks[idx+1:]...,
			)
		}
		lock := namedLock{
			Name:        name,
			OwnerJobID:  ownerJobID,
			CreatedAt:   now,
			HeartbeatAt: now,
		}
		snapshot.SerialLocks = append(snapshot.SerialLocks, lock)
		sortLocks(&snapshot)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"acquired":     true,
			"lock":         lock,
		}, http.StatusCreated, nil
	case "artifact":
		if idx := findNamedLock(snapshot.ArtifactLocks, name); idx >= 0 {
			existing := snapshot.ArtifactLocks[idx]
			if timeoutSec <= 0 || !lockHeartbeatIsStale(existing.HeartbeatAt, timeoutSec) {
				return lockHeldResponse(kind, name, existing.OwnerJobID), http.StatusConflict, nil
			}
			snapshot.ArtifactLocks = append(
				snapshot.ArtifactLocks[:idx],
				snapshot.ArtifactLocks[idx+1:]...,
			)
		}
		lock := namedLock{
			Name:        name,
			OwnerJobID:  ownerJobID,
			CreatedAt:   now,
			HeartbeatAt: now,
		}
		snapshot.ArtifactLocks = append(snapshot.ArtifactLocks, lock)
		sortLocks(&snapshot)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"acquired":     true,
			"lock":         lock,
		}, http.StatusCreated, nil
	default:
		return nil, 0, fmt.Errorf("unsupported lock kind %q", kind)
	}
}

func (s *server) heartbeatResourceLock(
	kind string,
	name string,
	ownerJobID string,
) (map[string]any, int, error) {
	s.locksMu.Lock()
	defer s.locksMu.Unlock()

	snapshot, err := s.readLocksSnapshotUnlocked()
	if err != nil {
		return nil, 0, err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	switch kind {
	case "dut":
		idx := findDutLock(snapshot.DutLocks, name)
		if idx < 0 {
			return lockMissingResponse(kind, name), http.StatusNotFound, nil
		}
		if snapshot.DutLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.DutLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.DutLocks[idx].HeartbeatAt = now
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"heartbeat":    true,
			"lock":         snapshot.DutLocks[idx],
		}, http.StatusOK, nil
	case "builder":
		idx := findBuilderLock(snapshot.BuilderLocks, name)
		if idx < 0 {
			return lockMissingResponse(kind, name), http.StatusNotFound, nil
		}
		if snapshot.BuilderLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.BuilderLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.BuilderLocks[idx].HeartbeatAt = now
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"heartbeat":    true,
			"lock":         snapshot.BuilderLocks[idx],
		}, http.StatusOK, nil
	case "serial":
		idx := findNamedLock(snapshot.SerialLocks, name)
		if idx < 0 {
			return lockMissingResponse(kind, name), http.StatusNotFound, nil
		}
		if snapshot.SerialLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.SerialLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.SerialLocks[idx].HeartbeatAt = now
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"heartbeat":    true,
			"lock":         snapshot.SerialLocks[idx],
		}, http.StatusOK, nil
	case "artifact":
		idx := findNamedLock(snapshot.ArtifactLocks, name)
		if idx < 0 {
			return lockMissingResponse(kind, name), http.StatusNotFound, nil
		}
		if snapshot.ArtifactLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.ArtifactLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.ArtifactLocks[idx].HeartbeatAt = now
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return map[string]any{
			"type":         kind,
			"name":         name,
			"owner_job_id": ownerJobID,
			"heartbeat":    true,
			"lock":         snapshot.ArtifactLocks[idx],
		}, http.StatusOK, nil
	default:
		return nil, 0, fmt.Errorf("unsupported lock kind %q", kind)
	}
}

func (s *server) releaseResourceLock(
	kind string,
	name string,
	ownerJobID string,
) (map[string]any, int, error) {
	s.locksMu.Lock()
	defer s.locksMu.Unlock()

	snapshot, err := s.readLocksSnapshotUnlocked()
	if err != nil {
		return nil, 0, err
	}
	switch kind {
	case "dut":
		idx := findDutLock(snapshot.DutLocks, name)
		if idx < 0 {
			return lockReleasedResponse(kind, name, ownerJobID, false), http.StatusOK, nil
		}
		if snapshot.DutLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.DutLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.DutLocks = append(snapshot.DutLocks[:idx], snapshot.DutLocks[idx+1:]...)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return lockReleasedResponse(kind, name, ownerJobID, true), http.StatusOK, nil
	case "builder":
		idx := findBuilderLock(snapshot.BuilderLocks, name)
		if idx < 0 {
			return lockReleasedResponse(kind, name, ownerJobID, false), http.StatusOK, nil
		}
		if snapshot.BuilderLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.BuilderLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.BuilderLocks = append(
			snapshot.BuilderLocks[:idx],
			snapshot.BuilderLocks[idx+1:]...,
		)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return lockReleasedResponse(kind, name, ownerJobID, true), http.StatusOK, nil
	case "serial":
		idx := findNamedLock(snapshot.SerialLocks, name)
		if idx < 0 {
			return lockReleasedResponse(kind, name, ownerJobID, false), http.StatusOK, nil
		}
		if snapshot.SerialLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.SerialLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.SerialLocks = append(
			snapshot.SerialLocks[:idx],
			snapshot.SerialLocks[idx+1:]...,
		)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return lockReleasedResponse(kind, name, ownerJobID, true), http.StatusOK, nil
	case "artifact":
		idx := findNamedLock(snapshot.ArtifactLocks, name)
		if idx < 0 {
			return lockReleasedResponse(kind, name, ownerJobID, false), http.StatusOK, nil
		}
		if snapshot.ArtifactLocks[idx].OwnerJobID != ownerJobID {
			return lockHeldResponse(kind, name, snapshot.ArtifactLocks[idx].OwnerJobID), http.StatusConflict, nil
		}
		snapshot.ArtifactLocks = append(
			snapshot.ArtifactLocks[:idx],
			snapshot.ArtifactLocks[idx+1:]...,
		)
		if err := s.writeLocksSnapshotUnlocked(snapshot); err != nil {
			return nil, 0, err
		}
		return lockReleasedResponse(kind, name, ownerJobID, true), http.StatusOK, nil
	default:
		return nil, 0, fmt.Errorf("unsupported lock kind %q", kind)
	}
}

func (s *server) readLocksSnapshot() (locksSnapshot, error) {
	s.locksMu.Lock()
	defer s.locksMu.Unlock()
	return s.readLocksSnapshotUnlocked()
}

func (s *server) readLocksSnapshotUnlocked() (locksSnapshot, error) {
	path := s.locksSnapshotPath()
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return emptyLocksSnapshot(), nil
		}
		return locksSnapshot{}, err
	}
	var snapshot locksSnapshot
	if err := json.Unmarshal(data, &snapshot); err != nil {
		return locksSnapshot{}, fmt.Errorf("locks.json is not valid JSON: %v", err)
	}
	normalizeLocksSnapshot(&snapshot)
	return snapshot, nil
}

func (s *server) writeLocksSnapshotUnlocked(snapshot locksSnapshot) error {
	normalizeLocksSnapshot(&snapshot)
	sortLocks(&snapshot)
	snapshot.GeneratedAt = time.Now().UTC().Format(time.RFC3339Nano)
	data, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(s.artifactsDir, 0o755); err != nil {
		return err
	}
	path := s.locksSnapshotPath()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

func (s *server) locksSnapshotPath() string {
	return filepath.Join(s.artifactsDir, "locks.json")
}

func emptyLocksSnapshot() locksSnapshot {
	return locksSnapshot{
		GeneratedAt:   "",
		DutLocks:      []dutLock{},
		BuilderLocks:  []builderLock{},
		SerialLocks:   []namedLock{},
		ArtifactLocks: []namedLock{},
	}
}

func normalizeLocksSnapshot(snapshot *locksSnapshot) {
	if snapshot.DutLocks == nil {
		snapshot.DutLocks = []dutLock{}
	}
	if snapshot.BuilderLocks == nil {
		snapshot.BuilderLocks = []builderLock{}
	}
	if snapshot.SerialLocks == nil {
		snapshot.SerialLocks = []namedLock{}
	}
	if snapshot.ArtifactLocks == nil {
		snapshot.ArtifactLocks = []namedLock{}
	}
}

func sortLocks(snapshot *locksSnapshot) {
	sort.Slice(snapshot.DutLocks, func(i, j int) bool {
		return snapshot.DutLocks[i].DutName < snapshot.DutLocks[j].DutName
	})
	sort.Slice(snapshot.BuilderLocks, func(i, j int) bool {
		return snapshot.BuilderLocks[i].BuilderName < snapshot.BuilderLocks[j].BuilderName
	})
	sort.Slice(snapshot.SerialLocks, func(i, j int) bool {
		return snapshot.SerialLocks[i].Name < snapshot.SerialLocks[j].Name
	})
	sort.Slice(snapshot.ArtifactLocks, func(i, j int) bool {
		return snapshot.ArtifactLocks[i].Name < snapshot.ArtifactLocks[j].Name
	})
}

func findDutLock(locks []dutLock, name string) int {
	for i, lock := range locks {
		if lock.DutName == name {
			return i
		}
	}
	return -1
}

func findBuilderLock(locks []builderLock, name string) int {
	for i, lock := range locks {
		if lock.BuilderName == name {
			return i
		}
	}
	return -1
}

func findNamedLock(locks []namedLock, name string) int {
	for i, lock := range locks {
		if lock.Name == name {
			return i
		}
	}
	return -1
}

func lockHeldResponse(kind string, name string, owner string) map[string]any {
	return map[string]any{
		"error":        "lock held",
		"type":         kind,
		"name":         name,
		"owner_job_id": owner,
		"acquired":     false,
	}
}

func lockMissingResponse(kind string, name string) map[string]any {
	return map[string]any{
		"error": "lock not found",
		"type":  kind,
		"name":  name,
	}
}

func lockReleasedResponse(kind string, name string, owner string, released bool) map[string]any {
	return map[string]any{
		"type":         kind,
		"name":         name,
		"owner_job_id": owner,
		"released":     released,
	}
}

func lockHeartbeatIsStale(heartbeat string, timeoutSec int) bool {
	timestamp, err := parseLockTimestamp(heartbeat)
	if err != nil {
		return true
	}
	return timestamp.Before(time.Now().UTC().Add(-time.Duration(timeoutSec) * time.Second))
}

func parseLockTimestamp(raw string) (time.Time, error) {
	timestamp, err := time.Parse(time.RFC3339Nano, raw)
	if err == nil {
		return timestamp, nil
	}
	timestamp, err = time.Parse("2006-01-02T15:04:05.999999999", raw)
	if err == nil {
		return timestamp.UTC(), nil
	}
	timestamp, err = time.Parse("2006-01-02T15:04:05", raw)
	if err == nil {
		return timestamp.UTC(), nil
	}
	return time.Time{}, err
}

func isSafeLockName(name string) bool {
	if name == "" || name == "." || name == ".." || len(name) > 128 {
		return false
	}
	for _, r := range name {
		switch {
		case r < 0x20 || r == 0x7f:
			return false
		case r == '/' || r == '\\':
			return false
		}
	}
	return true
}
