package state

import "testing"

func TestValid(t *testing.T) {
	valid := []string{"PENDING", "BUILD_RUNNING", "ARTIFACT_EXPORTED", "SUCCEEDED", "FAILED", "CANCELLED", "DRY_RUN"}
	for _, s := range valid {
		if !Valid(s) {
			t.Errorf("Valid(%q) = false, want true", s)
		}
	}
	for _, s := range []string{"", "bogus", "succeeded", "BUILD"} {
		if Valid(s) {
			t.Errorf("Valid(%q) = true, want false", s)
		}
	}
}

func TestClassificationSets(t *testing.T) {
	if !ResumableFrom[BuildSucceeded] || !ResumableFrom[ArtifactSelected] || !ResumableFrom[ArtifactExported] {
		t.Error("ResumableFrom missing an expected state")
	}
	if ResumableFrom[Pending] {
		t.Error("PENDING should not be resumable")
	}
	if !NonResumableTerminal[Succeeded] || !NonResumableTerminal[DryRun] || !NonResumableTerminal[Cancelled] {
		t.Error("NonResumableTerminal missing an expected state")
	}
	if NonResumableTerminal[Failed] {
		t.Error("FAILED is resumable, must not be in NonResumableTerminal")
	}
}
