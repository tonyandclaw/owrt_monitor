package main

import (
	"io"
	"net/http"
)

const defaultDaemonURL = "http://127.0.0.1:8765"

type client struct {
	baseURL string
	http    *http.Client
}

type jobEntry struct {
	JobID      string `json:"job_id"`
	State      string `json:"state"`
	Success    bool   `json:"success"`
	DryRun     bool   `json:"dry_run"`
	StartedAt  string `json:"started_at,omitempty"`
	FinishedAt string `json:"finished_at,omitempty"`
	RunDir     string `json:"run_dir"`
}

type runnerOutputEvent struct {
	TS     string `json:"ts"`
	Stream string `json:"stream"`
	Line   string `json:"line"`
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

type runnerStatus struct {
	JobID      string `json:"job_id"`
	Status     string `json:"status"`
	PID        int    `json:"pid,omitempty"`
	ExitCode   *int   `json:"exit_code,omitempty"`
	Error      string `json:"error,omitempty"`
	UpdatedAt  string `json:"updated_at"`
	FinishedAt string `json:"finished_at,omitempty"`
}

type cli struct {
	stdout io.Writer
	stderr io.Writer
	client *http.Client
}
