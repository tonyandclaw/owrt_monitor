// Package workflow is the Go port of the engine's workflow state machine
// (python/owrt_monitor/workflow.py). BuildWorkflow drives PENDING → PREFLIGHT →
// BUILD_RUNNING → BUILD_SUCCEEDED → ARTIFACT_SELECTED → ARTIFACT_EXPORTED →
// SUCCEEDED, persisting every transition to SQLite and events.jsonl before side
// effects, and writing report.json/report.md at the end — the same on-disk
// contract the Python engine and the Go daemon use.
//
// The destructive flash path (DutWorkflow) is hardware-coupled and not yet
// ported; Run rejects allow_flash with a clear error rather than pretending.
package workflow

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/tonyandclaw/owrt_monitor/internal/artifact"
	"github.com/tonyandclaw/owrt_monitor/internal/buildlog"
	"github.com/tonyandclaw/owrt_monitor/internal/config"
	"github.com/tonyandclaw/owrt_monitor/internal/docker"
	"github.com/tonyandclaw/owrt_monitor/internal/events"
	"github.com/tonyandclaw/owrt_monitor/internal/report"
	"github.com/tonyandclaw/owrt_monitor/internal/state"
	"github.com/tonyandclaw/owrt_monitor/internal/store"
	"gopkg.in/yaml.v3"
)

// Error is raised when a workflow cannot complete (workflow.WorkflowError).
type Error struct{ msg string }

func (e *Error) Error() string { return e.msg }

func wfErr(format string, args ...any) *Error { return &Error{msg: fmt.Sprintf(format, args...)} }

// BuildWorkflow runs the build → artifact-export pipeline.
type BuildWorkflow struct {
	configPath   string
	config       *config.OwrtConfig
	profile      *string
	artifactRoot string
	store        *store.Store
	docker       docker.BuildClient // nil → built from config at run time
}

// NewBuildWorkflow loads the config (applying the effective profile), opens the
// shared SQLite store, and returns a ready workflow. Call Close when done.
func NewBuildWorkflow(configPath string, profile *string, dockerClient docker.BuildClient) (*BuildWorkflow, error) {
	abs, err := filepath.Abs(configPath)
	if err != nil {
		return nil, err
	}
	cfg, err := config.Load(abs)
	if err != nil {
		return nil, err
	}
	eff := cfg.EffectiveProfile(profile)
	if eff != nil {
		cfg, err = cfg.WithProfile(*eff)
		if err != nil {
			return nil, err
		}
	}
	st, err := store.Open(cfg.StateDBPath(abs))
	if err != nil {
		return nil, err
	}
	return &BuildWorkflow{
		configPath:   abs,
		config:       cfg,
		profile:      eff,
		artifactRoot: cfg.ArtifactRoot(abs),
		store:        st,
		docker:       dockerClient,
	}, nil
}

// Close releases the store handle.
func (w *BuildWorkflow) Close() error {
	if w.store != nil {
		return w.store.Close()
	}
	return nil
}

func (w *BuildWorkflow) dockerClient() docker.BuildClient {
	if w.docker != nil {
		return w.docker
	}
	return docker.New(w.config.Builder)
}

// Run executes the workflow. allowFlash is not yet implemented in the Go engine.
func (w *BuildWorkflow) Run(dryRun, allowFlash bool) (rep *report.Report, err error) {
	if allowFlash {
		return nil, wfErr("the Go engine does not yet implement the flash/DUT path; use --dry-run or the Python engine for --allow-flash")
	}

	jobID, err := newJobID()
	if err != nil {
		return nil, err
	}
	runDir := filepath.Join(w.artifactRoot, jobID)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		return nil, err
	}

	snapshot, err := w.config.RedactedDump()
	if err != nil {
		return nil, err
	}
	if err := writeConfigSnapshot(filepath.Join(runDir, "config.snapshot.yaml"), snapshot); err != nil {
		return nil, err
	}
	snapshotJSON, err := json.Marshal(snapshot)
	if err != nil {
		return nil, err
	}

	if err := w.store.CreateJob(store.Job{
		ID: jobID, ConfigPath: w.configPath, ArtifactDir: runDir,
		State: string(state.Pending), ConfigSnapshot: string(snapshotJSON),
		PID: intPtr(os.Getpid()),
	}); err != nil {
		return nil, err
	}

	logger, err := events.New(w.store, jobID, filepath.Join(runDir, "events.jsonl"))
	if err != nil {
		return nil, err
	}
	rep = &report.Report{
		JobID: jobID, State: string(state.Pending), DryRun: dryRun, RunDir: runDir,
	}

	builderName := w.config.Builder.Container
	lockHeld := false
	defer func() {
		if lockHeld {
			_ = w.store.ReleaseBuilderLock(builderName, jobID)
		}
	}()

	fail := func(cause error) (*report.Report, error) {
		rep.State = string(state.Failed)
		rep.Success = false
		rep.Warnings = append(rep.Warnings, cause.Error())
		_ = w.store.FinishJob(jobID, string(state.Failed), "failed")
		_ = logger.Emit("ERROR", "workflow", "job_failed", cause.Error(),
			map[string]any{"run_dir": runDir})
		_ = rep.Write()
		return rep, wfErr("%s", cause.Error())
	}

	lockTimeout := w.config.Builder.LockTimeoutSec
	ok, err := w.store.AcquireBuilderLock(builderName, jobID, &lockTimeout)
	if err != nil {
		return fail(err)
	}
	if !ok {
		owner, _, _ := w.store.BuilderLockOwner(builderName)
		return fail(wfErr("builder %q is busy (held by job %s); wait for it to finish or use a different builder.container", builderName, owner))
	}
	lockHeld = true

	dc := w.dockerClient()
	w.transition(logger, jobID, state.Preflight, "starting preflight checks", nil)

	buildCmd := shellJoin(dc.BuildCommand(true))
	rep.Actions = append(rep.Actions, fmt.Sprintf("Build command: `%s`", buildCmd))
	rep.Actions = append(rep.Actions, "Artifact search patterns: "+backquoteJoin(w.config.Artifact.Patterns))

	if dryRun {
		rep.State = string(state.DryRun)
		rep.Success = true
		_ = w.store.FinishJob(jobID, string(state.DryRun), "dry-run")
		_ = logger.Emit("INFO", "workflow", "dry_run_completed",
			"validated config and planned actions without external side effects",
			map[string]any{"run_dir": runDir})
		if err := rep.Write(); err != nil {
			return nil, err
		}
		return rep, nil
	}

	if err := dc.Preflight(); err != nil {
		return fail(err)
	}

	w.transition(logger, jobID, state.BuildRunning, "OpenWrt build started", nil)
	buildLogPath := filepath.Join(runDir, "build.log")
	buildStart := time.Now()
	if err := dc.RunBuild(context.Background(), buildLogPath); err != nil {
		summary := buildlog.Classify(buildLogPath)
		rep.BuildSummary = summary.ToMap()
		_ = logger.Emit("WARN", "build_log", "build_log_classified",
			"build log classified as "+summary.Classification, summary.ToMap())
		return fail(err)
	}
	summary := buildlog.Classify(buildLogPath)
	rep.BuildSummary = summary.ToMap()
	_ = logger.Emit("INFO", "build_log", "build_log_classified",
		"build log classified as "+summary.Classification, summary.ToMap())
	buildDuration := time.Since(buildStart).Seconds()
	if summary.DurationSec != nil {
		buildDuration = *summary.DurationSec
	}
	w.attachBuildMetadata(rep, dc, logger)
	w.transition(logger, jobID, state.BuildSucceeded, "OpenWrt build succeeded", nil)

	exported, err := w.selectAndExport(dc, runDir, jobID, logger)
	if err != nil {
		return fail(err)
	}
	rep.Artifact = exported

	metrics := map[string]any{"build_duration_sec": buildDuration}
	rep.Metrics = metrics
	metricsJSON, _ := json.Marshal(metrics)

	rep.State = string(state.Succeeded)
	rep.Success = true
	if err := w.store.FinishJob(jobID, string(state.Succeeded), "success"); err != nil {
		return fail(err)
	}
	_ = w.store.SetMetrics(jobID, string(metricsJSON))
	_ = logger.Emit("INFO", "workflow", "job_succeeded", "build and artifact export completed",
		map[string]any{"run_dir": runDir, "metrics": metrics})
	if err := rep.Write(); err != nil {
		return nil, err
	}
	return rep, nil
}

func (w *BuildWorkflow) selectAndExport(dc docker.BuildClient, runDir, jobID string, logger *events.Logger) (*artifact.ExportedArtifact, error) {
	candidates, err := dc.ListArtifacts(w.config.Artifact.Patterns)
	if err != nil {
		return nil, err
	}
	selected, err := artifact.Select(candidates, w.config.Artifact.Selection,
		w.config.Artifact.MinSizeMB, w.config.Artifact.RegexPatterns)
	if err != nil {
		return nil, err
	}
	w.transition(logger, jobID, state.ArtifactSelected, "selected firmware artifact",
		map[string]any{"path": selected.Path, "size_bytes": selected.SizeBytes})

	filename := selected.Filename()
	if w.config.Artifact.ExportFilename != nil && *w.config.Artifact.ExportFilename != "" {
		filename = *w.config.Artifact.ExportFilename
	}
	hostPath := filepath.Join(runDir, filename)
	exported, err := dc.CopyArtifact(selected, hostPath)
	if err != nil {
		return nil, err
	}
	if w.config.Artifact.RequireSHA256 && exported.SHA256 == "" {
		return nil, wfErr("artifact %s has no SHA256 but require_sha256 is set", exported.Filename)
	}
	if err := w.store.RecordArtifact(store.Artifact{
		JobID: jobID, ContainerPath: exported.ContainerPath, HostPath: exported.HostPath,
		Filename: exported.Filename, SizeBytes: exported.SizeBytes, SHA256: exported.SHA256,
	}); err != nil {
		return nil, err
	}
	w.transition(logger, jobID, state.ArtifactExported, "exported firmware artifact to host",
		map[string]any{"host_path": exported.HostPath, "sha256": exported.SHA256})
	return &exported, nil
}

func (w *BuildWorkflow) attachBuildMetadata(rep *report.Report, dc docker.BuildClient, logger *events.Logger) {
	var makeTarget any
	if len(w.config.Builder.Command) > 1 {
		makeTarget = w.config.Builder.Command[len(w.config.Builder.Command)-1]
	}
	meta := map[string]any{
		"built_at":    store.NowISO(),
		"make_target": makeTarget,
		"profile":     ptrToAny(w.profile),
	}
	for k, v := range dc.GatherBuildMetadata() {
		meta[k] = v
	}
	rep.BuildMetadata = meta
	_ = logger.Emit("INFO", "build_metadata", "build_metadata_captured", "captured build provenance", meta)
}

func (w *BuildWorkflow) transition(logger *events.Logger, jobID string, s state.Job, message string, fields map[string]any) {
	_ = w.store.SetState(jobID, string(s))
	merged := map[string]any{"state": string(s)}
	for k, v := range fields {
		merged[k] = v
	}
	_ = logger.Emit("INFO", "workflow", "state_transition", message, merged)
}

var jobIDPattern = regexp.MustCompile(`[^A-Za-z0-9_-]`)

func newJobID() (string, error) {
	if configured := os.Getenv("OWRT_MONITOR_JOB_ID"); configured != "" {
		if len(configured) > 80 || jobIDPattern.MatchString(configured) {
			return "", wfErr("OWRT_MONITOR_JOB_ID may contain only alphanumerics, underscore, or hyphen")
		}
		return configured, nil
	}
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "job_" + hex.EncodeToString(b), nil
}

func writeConfigSnapshot(path string, snapshot map[string]any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := yaml.Marshal(snapshot)
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

func intPtr(v int) *int { return &v }

func ptrToAny(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}

// shellJoin renders a command for display, single-quoting args that need it
// (approximates shlex.quote for the report's build-command action).
func shellJoin(args []string) string {
	parts := make([]string, len(args))
	for i, a := range args {
		parts[i] = shellQuote(a)
	}
	return strings.Join(parts, " ")
}

var safeArg = regexp.MustCompile(`^[A-Za-z0-9_@%+=:,./-]+$`)

func shellQuote(s string) string {
	if s != "" && safeArg.MatchString(s) {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

func backquoteJoin(items []string) string {
	parts := make([]string, len(items))
	for i, p := range items {
		parts[i] = "`" + p + "`"
	}
	return strings.Join(parts, ", ")
}
