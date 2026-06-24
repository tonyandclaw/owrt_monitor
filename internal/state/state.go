// Package state mirrors python/owrt_monitor/state.py: the workflow job states
// and the resume/terminal classifications used by the engine.
package state

// Job is one workflow state. Values match state.py exactly so transitions are
// readable across both engines (they share events.jsonl and the jobs table).
type Job string

const (
	Pending             Job = "PENDING"
	Preflight           Job = "PREFLIGHT"
	BuildRunning        Job = "BUILD_RUNNING"
	BuildSucceeded      Job = "BUILD_SUCCEEDED"
	ArtifactSelected    Job = "ARTIFACT_SELECTED"
	ArtifactExported    Job = "ARTIFACT_EXPORTED"
	DutLocked           Job = "DUT_LOCKED"
	DutReady            Job = "DUT_READY"
	FirmwareTransferred Job = "FIRMWARE_TRANSFERRED"
	UpgradeRunning      Job = "UPGRADE_RUNNING"
	RebootWait          Job = "REBOOT_WAIT"
	DutOnline           Job = "DUT_ONLINE"
	TestRunning         Job = "TEST_RUNNING"
	DryRun              Job = "DRY_RUN"
	Succeeded           Job = "SUCCEEDED"
	Failed              Job = "FAILED"
	Cancelled           Job = "CANCELLED"
)

// ResumableFrom is the set of last-progress states a job can resume from
// (workflow.py RESUMABLE_FROM).
var ResumableFrom = map[Job]bool{
	BuildSucceeded:   true,
	ArtifactSelected: true,
	ArtifactExported: true,
}

// NonResumableTerminal is the set of terminal states that cannot resume
// (workflow.py NON_RESUMABLE_TERMINAL).
var NonResumableTerminal = map[Job]bool{
	Succeeded: true,
	DryRun:    true,
	Cancelled: true,
}

// Valid reports whether s is a known job state.
func Valid(s string) bool {
	switch Job(s) {
	case Pending, Preflight, BuildRunning, BuildSucceeded, ArtifactSelected,
		ArtifactExported, DutLocked, DutReady, FirmwareTransferred, UpgradeRunning,
		RebootWait, DutOnline, TestRunning, DryRun, Succeeded, Failed, Cancelled:
		return true
	}
	return false
}
