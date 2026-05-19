package main

import (
	"os"
	"sync"
	"time"
)

type healthResponse struct {
	Status string `json:"status"`
}

type errorResponse struct {
	Error string `json:"error"`
}

// jobsListEntry is the trimmed shape returned by /v1/jobs. Mirrors the
// most-useful fields from the Python `report.json` so a UI can render a
// list without reading every report.
type jobsListEntry struct {
	JobID      string `json:"job_id"`
	State      string `json:"state"`
	Success    bool   `json:"success"`
	DryRun     bool   `json:"dry_run"`
	StartedAt  string `json:"started_at,omitempty"`
	FinishedAt string `json:"finished_at,omitempty"`
	RunDir     string `json:"run_dir"`
	sortKey    string `json:"-"`
}

type jobSubmitRequest struct {
	Command    string `json:"command"`
	Config     string `json:"config"`
	Profile    string `json:"profile,omitempty"`
	DryRun     bool   `json:"dry_run,omitempty"`
	AllowFlash bool   `json:"allow_flash,omitempty"`
	Artifact   string `json:"artifact,omitempty"`
	WorkingDir string `json:"working_dir,omitempty"`
}

type jobSubmitResponse struct {
	JobID        string   `json:"job_id"`
	PID          int      `json:"pid"`
	Status       string   `json:"status"`
	Command      []string `json:"command"`
	RunDir       string   `json:"run_dir"`
	RunnerLog    string   `json:"runner_log"`
	RunnerOutput string   `json:"runner_output"`
}

type jobDeleteResponse struct {
	JobID   string `json:"job_id"`
	Removed bool   `json:"removed"`
	RunDir  string `json:"run_dir"`
}

type runnerStatus struct {
	JobID             string   `json:"job_id"`
	Status            string   `json:"status"`
	PID               int      `json:"pid,omitempty"`
	Command           []string `json:"command"`
	RunDir            string   `json:"run_dir"`
	RunnerLog         string   `json:"runner_log"`
	RunnerOutput      string   `json:"runner_output"`
	StartedAt         string   `json:"started_at"`
	UpdatedAt         string   `json:"updated_at"`
	FinishedAt        string   `json:"finished_at,omitempty"`
	CancelRequestedAt string   `json:"cancel_requested_at,omitempty"`
	OrphanedAt        string   `json:"orphaned_at,omitempty"`
	ExitCode          *int     `json:"exit_code,omitempty"`
	Error             string   `json:"error,omitempty"`
	OutputRotated     bool     `json:"output_rotated,omitempty"`
	OutputTruncated   bool     `json:"output_truncated,omitempty"`
}

type runnerOutputEvent struct {
	TS     string `json:"ts"`
	JobID  string `json:"job_id"`
	Stream string `json:"stream"`
	Line   string `json:"line"`
}

type locksSnapshot struct {
	GeneratedAt   string        `json:"generated_at"`
	DutLocks      []dutLock     `json:"dut_locks"`
	BuilderLocks  []builderLock `json:"builder_locks"`
	SerialLocks   []namedLock   `json:"serial_locks"`
	ArtifactLocks []namedLock   `json:"artifact_locks"`
}

type dutLock struct {
	DutName     string `json:"dut_name"`
	OwnerJobID  string `json:"owner_job_id"`
	CreatedAt   string `json:"created_at"`
	HeartbeatAt string `json:"heartbeat_at"`
}

type builderLock struct {
	BuilderName string `json:"builder_name"`
	OwnerJobID  string `json:"owner_job_id"`
	CreatedAt   string `json:"created_at"`
	HeartbeatAt string `json:"heartbeat_at"`
}

type namedLock struct {
	Name        string `json:"name"`
	OwnerJobID  string `json:"owner_job_id"`
	CreatedAt   string `json:"created_at"`
	HeartbeatAt string `json:"heartbeat_at"`
}

type lockMutationRequest struct {
	OwnerJobID     string `json:"owner_job_id"`
	LockTimeoutSec int    `json:"lock_timeout_sec,omitempty"`
}

type runnerOutputWriter struct {
	jobID          string
	statusPath     string
	humanPath      string
	structuredPath string
	human          *os.File
	structured     *os.File
	maxBytes       int64
	rotateBytes    int64
	rotateFiles    int
	bytesLogged    int64
	currentBytes   int64
	rotated        bool
	truncated      bool
	mu             sync.Mutex
}

type server struct {
	artifactsDir            string
	runnerBin               string
	runnerHeartbeatEvery    time.Duration
	runnerOutputMaxBytes    int64
	runnerOutputRotateBytes int64
	runnerOutputRotateFiles int
	processAlive            func(pid int) bool
	locksMu                 sync.Mutex
}
