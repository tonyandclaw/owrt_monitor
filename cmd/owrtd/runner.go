package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func captureRunnerOutput(
	writer *runnerOutputWriter,
	stream string,
	reader *os.File,
	done chan<- error,
) {
	defer reader.Close()
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		if err := writer.writeLine(stream, scanner.Text()); err != nil {
			done <- err
			return
		}
	}
	done <- scanner.Err()
}

func (w *runnerOutputWriter) writeLine(stream string, line string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	ts := time.Now().UTC().Format(time.RFC3339Nano)
	if w.maxBytes > 0 && w.bytesLogged >= w.maxBytes {
		if !w.truncated {
			return w.writeTruncationLocked(ts)
		}
		return nil
	}
	humanLine := fmt.Sprintf("%s %s %s\n", ts, stream, line)
	eventData, err := json.Marshal(runnerOutputEvent{
		TS:     ts,
		JobID:  w.jobID,
		Stream: stream,
		Line:   line,
	})
	if err != nil {
		return err
	}
	eventData = append(eventData, '\n')
	writeSize := int64(len(humanLine) + len(eventData))
	if w.maxBytes > 0 && w.bytesLogged+writeSize > w.maxBytes {
		if !w.truncated {
			return w.writeTruncationLocked(ts)
		}
		return nil
	}
	if w.rotateBytes > 0 && w.currentBytes > 0 && w.currentBytes+writeSize > w.rotateBytes {
		if err := w.rotateLocked(); err != nil {
			return err
		}
	}
	if _, err := io.WriteString(w.human, humanLine); err != nil {
		return err
	}
	if _, err := w.structured.Write(eventData); err != nil {
		return err
	}
	w.bytesLogged += writeSize
	w.currentBytes += writeSize
	return nil
}

func (w *runnerOutputWriter) writeTruncationLocked(ts string) error {
	line := fmt.Sprintf("runner output truncated after %d bytes", w.maxBytes)
	humanLine := fmt.Sprintf("%s runner %s\n", ts, line)
	eventData, err := json.Marshal(runnerOutputEvent{
		TS:     ts,
		JobID:  w.jobID,
		Stream: "runner",
		Line:   line,
	})
	if err != nil {
		return err
	}
	eventData = append(eventData, '\n')
	writeSize := int64(len(humanLine) + len(eventData))
	if w.rotateBytes > 0 && w.currentBytes > 0 && w.currentBytes+writeSize > w.rotateBytes {
		if err := w.rotateLocked(); err != nil {
			return err
		}
	}
	if _, err := io.WriteString(w.human, humanLine); err != nil {
		return err
	}
	if _, err := w.structured.Write(eventData); err != nil {
		return err
	}
	w.bytesLogged += writeSize
	w.currentBytes += writeSize
	w.truncated = true
	if err := markRunnerOutputTruncated(w.statusPath); err != nil {
		log.Printf("runner job %s output truncation status update: %v", w.jobID, err)
	}
	return nil
}

func (w *runnerOutputWriter) rotateLocked() error {
	if err := w.human.Close(); err != nil {
		return fmt.Errorf("close runner log before rotate: %w", err)
	}
	if err := w.structured.Close(); err != nil {
		return fmt.Errorf("close runner output before rotate: %w", err)
	}
	human, err := rotateRunnerOutputFile(w.humanPath, w.rotateFiles)
	if err != nil {
		return err
	}
	structured, err := rotateRunnerOutputFile(w.structuredPath, w.rotateFiles)
	if err != nil {
		_ = human.Close()
		return err
	}
	w.human = human
	w.structured = structured
	w.currentBytes = 0
	w.rotated = true
	if err := markRunnerOutputRotated(w.statusPath); err != nil {
		log.Printf("runner job %s output rotation status update: %v", w.jobID, err)
	}
	return nil
}

func rotateRunnerOutputFile(path string, keep int) (*os.File, error) {
	if keep < 1 {
		keep = 1
	}
	oldest := fmt.Sprintf("%s.%d", path, keep)
	if err := os.Remove(oldest); err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("remove rotated output %s: %w", filepath.Base(oldest), err)
	}
	for i := keep; i >= 1; i-- {
		src := path
		if i > 1 {
			src = fmt.Sprintf("%s.%d", path, i-1)
		}
		dst := fmt.Sprintf("%s.%d", path, i)
		if _, err := os.Stat(src); err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			return nil, fmt.Errorf("stat output before rotate %s: %w", filepath.Base(src), err)
		}
		if err := os.Rename(src, dst); err != nil {
			return nil, fmt.Errorf("rotate output %s: %w", filepath.Base(src), err)
		}
	}
	return os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
}

func (w *runnerOutputWriter) isTruncated() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.truncated
}

func (w *runnerOutputWriter) isRotated() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.rotated
}

func (w *runnerOutputWriter) close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return errors.Join(w.human.Close(), w.structured.Close())
}

func (s *server) runnerOutputLimit() int64 {
	return s.runnerOutputMaxBytes
}

func (s *server) runnerOutputRotateLimit() int64 {
	return s.runnerOutputRotateBytes
}

func (s *server) runnerOutputRotateFileCount() int {
	if s.runnerOutputRotateFiles > 0 {
		return s.runnerOutputRotateFiles
	}
	return 1
}

func (s *server) runnerHeartbeatInterval() time.Duration {
	if s.runnerHeartbeatEvery > 0 {
		return s.runnerHeartbeatEvery
	}
	return 5 * time.Second
}

func (s *server) monitorRunner(
	jobID string,
	statusPath string,
	status runnerStatus,
	cmd *exec.Cmd,
	done <-chan error,
) {
	ticker := time.NewTicker(s.runnerHeartbeatInterval())
	defer ticker.Stop()
	current := status
	for {
		select {
		case err := <-done:
			finishedAt := time.Now().UTC().Format(time.RFC3339Nano)
			finalStatus := mergeRunnerCancellation(statusPath, current)
			finalStatus.Status = "exited"
			finalStatus.UpdatedAt = finishedAt
			finalStatus.FinishedAt = finishedAt
			if cmd.ProcessState != nil {
				exitCode := cmd.ProcessState.ExitCode()
				finalStatus.ExitCode = &exitCode
			}
			if err != nil {
				finalStatus.Error = err.Error()
				log.Printf("runner job %s exited: %v", jobID, err)
			}
			if writeErr := writeRunnerStatus(statusPath, finalStatus); writeErr != nil {
				log.Printf("runner job %s status write after exit: %v", jobID, writeErr)
			}
			return
		case <-ticker.C:
			current = mergeRunnerCancellation(statusPath, current)
			current.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
			if err := writeRunnerStatus(statusPath, current); err != nil {
				log.Printf("runner job %s heartbeat status write: %v", jobID, err)
			}
		}
	}
}

func mergeRunnerCancellation(statusPath string, current runnerStatus) runnerStatus {
	onDisk, ok := readRunnerStatusBestEffort(statusPath)
	if !ok {
		return current
	}
	if onDisk.OutputRotated {
		current.OutputRotated = true
	}
	if onDisk.OutputTruncated {
		current.OutputTruncated = true
	}
	if onDisk.CancelRequestedAt != "" {
		current.CancelRequestedAt = onDisk.CancelRequestedAt
		if current.Status == "running" || current.Status == "starting" {
			current.Status = "cancel_requested"
		}
	}
	return current
}

// handleJobByID dispatches `/v1/jobs/{id}` and its sub-resources.
//
// Method matrix:
//
//	GET  /v1/jobs/{id}             → report.json
//	DELETE /v1/jobs/{id}           → remove on-disk run directory
//	GET  /v1/jobs/{id}/analysis    → advisory analysis.json
//	GET  /v1/jobs/{id}/events      → events.jsonl stream
//	GET  /v1/jobs/{id}/runner      → runner.json status
//	GET  /v1/jobs/{id}/runner-output → structured stdout/stderr stream
//	POST /v1/jobs/{id}/cancel      → write cancel.flag marker

func (s *server) serveAnalysis(w http.ResponseWriter, jobID string) {
	path := filepath.Join(s.artifactsDir, jobID, "analysis.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeJSON(w, http.StatusNotFound, errorResponse{
				Error: "no analysis.json for that job",
			})
			return
		}
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("analysis.json is not valid JSON: %v", err),
		})
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *server) serveRunner(w http.ResponseWriter, jobID string) {
	path := filepath.Join(s.artifactsDir, jobID, "runner.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeJSON(w, http.StatusNotFound, errorResponse{
				Error: "no runner.json for that job",
			})
			return
		}
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
		return
	}
	var payload runnerStatus
	if err := json.Unmarshal(data, &payload); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error: fmt.Sprintf("runner.json is not valid JSON: %v", err),
		})
		return
	}
	payload = s.reconcileRunnerStatus(path, payload)
	writeJSON(w, http.StatusOK, payload)
}

func (s *server) reconcileRunnerStatus(path string, status runnerStatus) runnerStatus {
	if !runnerStatusMayBeActive(status.Status) {
		return status
	}
	if status.PID > 0 && s.runnerProcessAlive(status.PID) {
		return status
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	status.Status = "orphaned"
	status.UpdatedAt = now
	status.FinishedAt = now
	status.OrphanedAt = now
	if status.Error == "" {
		if status.PID > 0 {
			status.Error = fmt.Sprintf("runner pid %d is not alive", status.PID)
		} else {
			status.Error = "runner has no pid"
		}
	}
	if err := writeRunnerStatus(path, status); err != nil {
		log.Printf("runner status reconciliation write for %s: %v", status.JobID, err)
	}
	return status
}

func runnerStatusMayBeActive(status string) bool {
	return status == "starting" || status == "running" || status == "cancel_requested"
}

func (s *server) runnerProcessAlive(pid int) bool {
	if s.processAlive != nil {
		return s.processAlive(pid)
	}
	return processAlive(pid)
}

func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	process, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	err = process.Signal(syscall.Signal(0))
	return err == nil
}

func (s *server) serveRunnerOutput(w http.ResponseWriter, r *http.Request, jobID string) {
	path := filepath.Join(s.artifactsDir, jobID, "runner.output.jsonl")
	tail, err := parseTailParam(r.URL.Query().Get("tail"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: err.Error()})
		return
	}
	follow, err := parseBoolQuery(r.URL.Query().Get("follow"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: err.Error()})
		return
	}
	if _, err := os.Stat(path); err != nil {
		writeRunnerOutputFileError(w, err)
		return
	}
	paths := runnerOutputReadPaths(path)
	if follow {
		s.followRunnerOutput(w, r, jobID, path, paths, tail)
		return
	}
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	if tail > 0 {
		data, _, err := tailFilesLines(paths, tail)
		if err != nil {
			logOrWriteFileError(w, err, "runner.output.jsonl")
			return
		}
		if _, err := w.Write(data); err != nil {
			log.Printf("runner.output.jsonl tail write for %s: %v", jobID, err)
		}
		return
	}
	if _, err := copyFilesFromStart(paths, w); err != nil {
		logOrWriteFileError(w, err, "runner.output.jsonl")
		return
	}
}

func (s *server) followRunnerOutput(
	w http.ResponseWriter,
	r *http.Request,
	jobID string,
	path string,
	paths []string,
	tail int,
) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "streaming unsupported"})
		return
	}
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	var offset int64
	if tail > 0 {
		data, size, err := tailFilesLines(paths, tail)
		if err != nil {
			logOrWriteFileError(w, err, "runner.output.jsonl")
			return
		}
		if _, err := w.Write(data); err != nil {
			log.Printf("runner.output.jsonl follow tail write for %s: %v", jobID, err)
			return
		}
		offset = size
	} else {
		size, err := copyFilesFromStart(paths, w)
		if err != nil {
			logOrWriteFileError(w, err, "runner.output.jsonl")
			return
		}
		offset = size
	}
	flusher.Flush()
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
			size, err := copyFollowRunnerOutput(path, offset, w)
			if err != nil {
				log.Printf("runner.output.jsonl follow copy for %s: %v", jobID, err)
				return
			}
			if size > offset {
				offset = size
				flusher.Flush()
				continue
			}
			if !s.runnerIsActive(jobID) {
				return
			}
		}
	}
}

func runnerOutputReadPaths(path string) []string {
	rotated := []string{}
	for i := 1; i <= 64; i++ {
		candidate := fmt.Sprintf("%s.%d", path, i)
		if _, err := os.Stat(candidate); err == nil {
			rotated = append(rotated, candidate)
			continue
		}
		break
	}
	paths := make([]string, 0, len(rotated)+1)
	for i := len(rotated) - 1; i >= 0; i-- {
		paths = append(paths, rotated[i])
	}
	return append(paths, path)
}

func parseTailParam(raw string) (int, error) {
	if raw == "" {
		return 0, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value < 1 || value > 10000 {
		return 0, errors.New("tail must be an integer in [1, 10000]")
	}
	return value, nil
}

func parseBoolQuery(raw string) (bool, error) {
	if raw == "" {
		return false, nil
	}
	switch strings.ToLower(raw) {
	case "1", "true", "yes":
		return true, nil
	case "0", "false", "no":
		return false, nil
	default:
		return false, fmt.Errorf("follow must be one of true/false/1/0/yes/no")
	}
}

func tailFilesLines(paths []string, n int) ([]byte, int64, error) {
	ring := make([]string, n)
	count := 0
	var currentSize int64
	for i, path := range paths {
		f, err := os.Open(path)
		if err != nil {
			return nil, 0, err
		}
		info, err := f.Stat()
		if err != nil {
			_ = f.Close()
			return nil, 0, err
		}
		if i == len(paths)-1 {
			currentSize = info.Size()
		}
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		for scanner.Scan() {
			ring[count%n] = scanner.Text()
			count++
		}
		if err := scanner.Err(); err != nil {
			_ = f.Close()
			return nil, 0, err
		}
		if err := f.Close(); err != nil {
			return nil, 0, err
		}
	}
	start := 0
	total := count
	if count > n {
		start = count % n
		total = n
	}
	var builder strings.Builder
	for i := 0; i < total; i++ {
		line := ring[(start+i)%n]
		builder.WriteString(line)
		builder.WriteByte('\n')
	}
	return []byte(builder.String()), currentSize, nil
}

func copyFilesFromStart(paths []string, dst io.Writer) (int64, error) {
	var currentSize int64
	for _, path := range paths {
		size, err := copyFileFromOffset(path, 0, dst)
		if err != nil {
			return currentSize, err
		}
		currentSize = size
	}
	return currentSize, nil
}

func copyFollowRunnerOutput(path string, offset int64, dst io.Writer) (int64, error) {
	info, err := os.Stat(path)
	if err != nil {
		return offset, err
	}
	if offset <= info.Size() {
		return copyFileFromOffset(path, offset, dst)
	}
	rotated := path + ".1"
	if rotatedInfo, err := os.Stat(rotated); err == nil && offset <= rotatedInfo.Size() {
		if _, err := copyFileFromOffset(rotated, offset, dst); err != nil {
			return offset, err
		}
	}
	return copyFileFromOffset(path, 0, dst)
}

func copyFileFromOffset(path string, offset int64, dst io.Writer) (int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return offset, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return offset, err
	}
	if offset > info.Size() {
		offset = 0
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return offset, err
	}
	if _, err := io.Copy(dst, f); err != nil {
		return offset, err
	}
	return info.Size(), nil
}

func (s *server) runnerIsActive(jobID string) bool {
	status, ok := readRunnerStatusBestEffort(filepath.Join(s.artifactsDir, jobID, "runner.json"))
	if !ok {
		return false
	}
	return runnerStatusMayBeActive(status.Status)
}

func logOrWriteFileError(w http.ResponseWriter, err error, label string) {
	if errors.Is(err, os.ErrNotExist) {
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "no " + label + " for that job"})
		return
	}
	if _, writeErr := fmt.Fprintf(w, `{"error":%q}`+"\n", err.Error()); writeErr != nil {
		log.Printf("write file error response: %v", writeErr)
	}
}

func writeRunnerOutputFileError(w http.ResponseWriter, err error) {
	if errors.Is(err, os.ErrNotExist) {
		writeJSON(w, http.StatusNotFound, errorResponse{
			Error: "no runner.output.jsonl for that job",
		})
		return
	}
	writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
}

func writeRunnerStatus(path string, status runnerStatus) error {
	data, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
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

func markRunnerOutputTruncated(path string) error {
	status, ok := readRunnerStatusBestEffort(path)
	if !ok {
		return nil
	}
	status.OutputTruncated = true
	status.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	return writeRunnerStatus(path, status)
}

func markRunnerOutputRotated(path string) error {
	status, ok := readRunnerStatusBestEffort(path)
	if !ok {
		return nil
	}
	status.OutputRotated = true
	status.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	return writeRunnerStatus(path, status)
}

func readRunnerStatusBestEffort(path string) (runnerStatus, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return runnerStatus{}, false
	}
	var status runnerStatus
	if err := json.Unmarshal(data, &status); err != nil {
		return runnerStatus{}, false
	}
	return status, true
}
