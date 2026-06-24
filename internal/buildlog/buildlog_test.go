package buildlog

import (
	"os"
	"path/filepath"
	"testing"
)

func fixture(name string) string {
	return filepath.Join("..", "..", "tests", "fixtures", "build_logs", name)
}

func TestClassifySuccessFixture(t *testing.T) {
	if _, err := os.Stat(fixture("success_owrt2102_ap.log")); err != nil {
		t.Skipf("fixture missing: %v", err)
	}
	s := Classify(fixture("success_owrt2102_ap.log"))
	if s.Classification != "success" || !s.Success {
		t.Fatalf("classification = %s success = %v", s.Classification, s.Success)
	}
	if s.DurationSec == nil || *s.DurationSec <= 0 {
		t.Errorf("expected a parsed duration, got %v", s.DurationSec)
	}
}

func TestClassifyDiskFullFixture(t *testing.T) {
	if _, err := os.Stat(fixture("disk_full.log")); err != nil {
		t.Skipf("fixture missing: %v", err)
	}
	s := Classify(fixture("disk_full.log"))
	if s.Classification != "disk_full" || s.Success {
		t.Fatalf("classification = %s success = %v", s.Classification, s.Success)
	}
	if len(s.Evidence) == 0 {
		t.Error("disk_full should carry evidence lines")
	}
}

func TestClassifyMissing(t *testing.T) {
	s := Classify(filepath.Join(t.TempDir(), "nope.log"))
	if s.Classification != "missing_log" || s.Success {
		t.Errorf("missing log = %s / %v", s.Classification, s.Success)
	}
}

func writeLog(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "build.log")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestClassifyPackageFailure(t *testing.T) {
	log := "Compiling foo\n" +
		"make[3]: *** [package/feeds/mtk/flowtable/compile] Error 2\n" +
		"make[2]: *** [package/Makefile:115: package/feeds/mtk/flowtable/compile] Error 2\n" +
		"make: *** [include/owrt2102.mk:163: owrt2102.asus_eap5000_mt7987] Error 2\n"
	s := Classify(writeLog(t, log))
	if s.Classification != "failed_package" {
		t.Fatalf("classification = %s", s.Classification)
	}
	if s.FailedStep != "package/feeds/mtk/flowtable/compile" {
		t.Errorf("failed_step = %q", s.FailedStep)
	}
	if s.FailedPackage != "feeds/mtk/flowtable" {
		t.Errorf("failed_package = %q", s.FailedPackage)
	}
	if s.FailedTarget != "owrt2102.asus_eap5000_mt7987" {
		t.Errorf("failed_target = %q", s.FailedTarget)
	}
}

func TestClassifyCompileErrorAndUnknown(t *testing.T) {
	top := "make: *** [include/toplevel.mk:228: world] Error 2\n"
	if s := Classify(writeLog(t, top)); s.Classification != "compile_error" {
		t.Errorf("toplevel-only classification = %s", s.Classification)
	}
	if s := Classify(writeLog(t, "random noise\nstill building\n")); s.Classification != "unknown" {
		t.Errorf("unrecognised classification = %s", s.Classification)
	}
}

func TestClassifyWarningsCaptured(t *testing.T) {
	log := "WARNING: package foo has no license\n>>>> board Build done in: 01:02.500\n"
	s := Classify(writeLog(t, log))
	if s.Classification != "success" {
		t.Fatalf("classification = %s", s.Classification)
	}
	if len(s.Warnings) != 1 || s.Warnings[0] != "package foo has no license" {
		t.Errorf("warnings = %v", s.Warnings)
	}
	if s.DurationSec == nil || *s.DurationSec != 62.5 {
		t.Errorf("duration = %v, want 62.5", s.DurationSec)
	}
}

func TestToMapShape(t *testing.T) {
	s := Classify(writeLog(t, ">>>> b Build done in: 00:10.000\n"))
	m := s.ToMap()
	for _, k := range []string{"classification", "success", "duration_sec", "failed_target", "failed_step", "failed_package", "evidence", "warnings"} {
		if _, ok := m[k]; !ok {
			t.Errorf("ToMap missing key %q", k)
		}
	}
	if m["failed_target"] != nil {
		t.Errorf("failed_target should be null on success, got %v", m["failed_target"])
	}
}
