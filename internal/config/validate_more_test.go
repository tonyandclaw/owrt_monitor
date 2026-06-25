package config

import (
	"path/filepath"
	"testing"
)

// Each case trips exactly one validator branch, exercising the validate.go
// rules across dut/upgrade/tests/retry that the happy-path tests don't reach.
func TestValidatorBranches(t *testing.T) {
	base := "builder: {container: c, workdir: /w, command: [make]}\nartifact: {patterns: [x]}\n"
	cases := map[string]string{
		"dut baud":              base + "dut: {baud: 0}\n",
		"dut bytesize":          base + "dut: {bytesize: 9}\n",
		"dut parity":            base + "dut: {parity: weird}\n",
		"dut stopbits":          base + "dut: {stopbits: 3}\n",
		"dut newline":           base + "dut: {newline: cr}\n",
		"dut connect timeout":   base + "dut: {connect_timeout_sec: 0}\n",
		"dut bad regex":         base + "dut: {expected_artifact_pattern: '('}\n",
		"upgrade timeout":       base + "upgrade: {boot_timeout_sec: 0}\n",
		"upgrade http port":     base + "upgrade: {http_port: 70000}\n",
		"upgrade scp port":      base + "upgrade: {scp_port: 0}\n",
		"upgrade scp blank":     base + "upgrade: {scp_user: '   '}\n",
		"bootloader timeout":    base + "upgrade: {bootloader: {autoboot_wait_sec: 0}}\n",
		"tests cmd timeout":     base + "tests: {command_timeout_sec: 0}\n",
		"smoke blank":           base + "tests: {smoke: ['  ']}\n",
		"smoke bad regex":       base + "tests: {smoke: [{command: x, expect: '('}]}\n",
		"script timeout":        base + "tests: {scripts: [{name: a, path: /p, timeout_sec: 0}]}\n",
		"ssh port":              base + "tests: {ssh: [{name: a, command: c, port: 0}]}\n",
		"retry attempts":        base + "retry: {smoke_tests: {attempts: 0}}\n",
		"retry backoff":         base + "retry: {smoke_tests: {backoff_sec: -1}}\n",
		"default profile blank": base + "project: {default_profile: '  '}\n",
		"on_profile_switch bad": "builder: {container: c, workdir: /w, command: [make], on_profile_switch: nuke}\nartifact: {patterns: [x]}\n",
		"empty cleanup command": "builder: {container: c, workdir: /w, command: [make], profile_switch_cleanup: [[]]}\nartifact: {patterns: [x]}\n",
	}
	for name, body := range cases {
		path := writeTemp(t, body)
		if _, err := Load(path); err == nil {
			t.Errorf("%s: expected validation error", name)
		}
	}
}

func TestEffectiveProfileAndList(t *testing.T) {
	cfg, err := Load(filepath.Join("..", "..", "configs", "example.yaml"))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	// No explicit request → config default_profile.
	if eff := cfg.EffectiveProfile(nil); eff == nil || *eff != "ap-be5000" {
		t.Errorf("EffectiveProfile(nil) = %v", eff)
	}
	// Explicit request wins.
	want := "controller"
	if eff := cfg.EffectiveProfile(&want); eff == nil || *eff != "controller" {
		t.Errorf("EffectiveProfile(controller) = %v", eff)
	}
	if got := cfg.ListProfiles(); len(got) != 7 || got[0] != "ap-be14000" {
		t.Errorf("ListProfiles = %v", got)
	}
}

func TestRedactedDumpNoSecrets(t *testing.T) {
	path := writeTemp(t, minimalYAML)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	dump, err := cfg.RedactedDump()
	if err != nil {
		t.Fatalf("RedactedDump: %v", err)
	}
	// password defaults to null → not masked, stays absent/null.
	dut := dump["dut"].(map[string]any)
	login := dut["login"].(map[string]any)
	if login["password"] != nil {
		t.Errorf("password should be null, got %v", login["password"])
	}
}

func TestDefaultProfileMustExist(t *testing.T) {
	path := writeTemp(t, `
project: {default_profile: ghost}
builder: {container: c, workdir: /w, command: [make]}
artifact: {patterns: [x]}
`)
	if _, err := Load(path); err == nil {
		t.Error("default_profile referencing an undefined profile should error")
	}
}

func TestInterpolationInListsAndMaps(t *testing.T) {
	t.Setenv("OWRT_IMG", "openwrt-test.bin")
	path := writeTemp(t, `
builder: {container: c, workdir: /w, command: [make, "${OWRT_IMG}"]}
artifact: {patterns: ["bin/${OWRT_IMG}"]}
`)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Builder.Command[1] != "openwrt-test.bin" {
		t.Errorf("list interpolation failed: %v", cfg.Builder.Command)
	}
	if cfg.Artifact.Patterns[0] != "bin/openwrt-test.bin" {
		t.Errorf("nested interpolation failed: %v", cfg.Artifact.Patterns)
	}
}
