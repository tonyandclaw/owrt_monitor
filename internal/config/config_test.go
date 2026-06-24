package config

import (
	"os"
	"path/filepath"
	"testing"
)

const minimalYAML = `
builder:
  container: openwrtbuild
  workdir: /work
  command: [make]
artifact:
  patterns: ["bin/targets/**/openwrt-*-sysupgrade.bin"]
`

func writeTemp(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

// TestLoadExampleConfig is the interop anchor: the Go loader must accept the
// repo's canonical configs/example.yaml that the Python engine also loads.
func TestLoadExampleConfig(t *testing.T) {
	cfg, err := Load(filepath.Join("..", "..", "configs", "example.yaml"))
	if err != nil {
		t.Fatalf("Load(example.yaml): %v", err)
	}
	if cfg.Project.Name != "owrt-monitor-lab" {
		t.Errorf("project.name = %q", cfg.Project.Name)
	}
	if cfg.Project.DefaultProfile == nil || *cfg.Project.DefaultProfile != "ap-be5000" {
		t.Errorf("default_profile = %v", cfg.Project.DefaultProfile)
	}
	if cfg.Builder.Container != "openwrtbuild" {
		t.Errorf("builder.container = %q", cfg.Builder.Container)
	}
	if cfg.Artifact.Selection != "fail-if-multiple" {
		t.Errorf("artifact.selection = %q", cfg.Artifact.Selection)
	}
	// Env interpolation default applied (var unset).
	if cfg.Dut.Serial == nil || *cfg.Dut.Serial != "/dev/cu.usbserial-8340" {
		t.Errorf("dut.serial = %v, want interpolated default", cfg.Dut.Serial)
	}
	if got := len(cfg.Profiles); got != 7 {
		t.Errorf("len(profiles) = %d, want 7", got)
	}
	// Smoke list mixes bare strings and dicts.
	if len(cfg.Tests.Smoke) != 6 {
		t.Fatalf("len(smoke) = %d, want 6", len(cfg.Tests.Smoke))
	}
	if !cfg.Tests.Smoke[0].Enabled || cfg.Tests.Smoke[0].Command != "ubus call system board" {
		t.Errorf("smoke[0] = %+v", cfg.Tests.Smoke[0])
	}
	if cfg.Tests.Smoke[3].Expect == nil {
		t.Errorf("smoke[3] should carry an expect regex")
	}
}

func TestWithProfileMerge(t *testing.T) {
	cfg, err := Load(filepath.Join("..", "..", "configs", "example.yaml"))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	merged, err := cfg.WithProfile("ap-be5000")
	if err != nil {
		t.Fatalf("WithProfile: %v", err)
	}
	// List replaced wholesale.
	want := []string{"make", "owrt2102.asus_eap5000_mt7987"}
	if len(merged.Builder.Command) != 2 || merged.Builder.Command[1] != want[1] {
		t.Errorf("builder.command = %v, want %v", merged.Builder.Command, want)
	}
	// Nested dict merged in from overlay.
	if !merged.Upgrade.NetworkRecovery.Enabled {
		t.Error("upgrade.network_recovery.enabled should be true after merge")
	}
	if merged.Dut.ExpectedArtifactPattern == nil || *merged.Dut.ExpectedArtifactPattern != "ASUS-EAP5000" {
		t.Errorf("dut.expected_artifact_pattern = %v", merged.Dut.ExpectedArtifactPattern)
	}
	// Base scalar preserved where overlay is silent.
	if merged.Upgrade.Transfer != "tftp" {
		t.Errorf("upgrade.transfer = %q, want tftp (from base)", merged.Upgrade.Transfer)
	}
	// Profiles cleared and default_profile reset on the merged result.
	if len(merged.Profiles) != 0 || merged.Project.DefaultProfile != nil {
		t.Errorf("merged should drop profiles/default_profile: %v / %v",
			merged.Profiles, merged.Project.DefaultProfile)
	}

	if _, err := cfg.WithProfile("does-not-exist"); err == nil {
		t.Error("WithProfile(unknown) should error")
	}
}

func TestEnvInterpolation(t *testing.T) {
	if got, err := interpolateString("${X:-fallback}"); err != nil || got != "fallback" {
		t.Errorf("default branch = %q, %v", got, err)
	}
	t.Setenv("OWRT_TEST_VAR", "live")
	if got, err := interpolateString("a-${OWRT_TEST_VAR}-b"); err != nil || got != "a-live-b" {
		t.Errorf("env branch = %q, %v", got, err)
	}
	if got, err := interpolateString("${OWRT_REQUIRED_UNSET}"); err == nil {
		t.Errorf("missing required var should error, got %q", got)
	}
	// Empty explicit default is honored (distinct from "no default").
	if got, err := interpolateString("${UNSET_VAR:-}"); err != nil || got != "" {
		t.Errorf("empty default = %q, %v", got, err)
	}
}

func TestUnknownKeyRejected(t *testing.T) {
	path := writeTemp(t, minimalYAML+"\nbogus_top_level: 1\n")
	if _, err := Load(path); err == nil {
		t.Error("unknown top-level key should be rejected (extra=forbid)")
	}
}

func TestValidationErrors(t *testing.T) {
	cases := map[string]string{
		"missing builder": "artifact:\n  patterns: [x]\n",
		"empty patterns":  minimalYAML + "\n", // overridden below
		"bad selection": `
builder: {container: c, workdir: /w, command: [make]}
artifact: {patterns: [x], selection: bogus}
`,
		"custom needs command": `
builder: {container: c, workdir: /w, command: [make]}
artifact: {patterns: [x]}
upgrade: {transfer: custom}
`,
	}
	// "empty patterns" needs a config with empty patterns list.
	cases["empty patterns"] = `
builder: {container: c, workdir: /w, command: [make]}
artifact: {patterns: []}
`
	for name, body := range cases {
		path := writeTemp(t, body)
		if _, err := Load(path); err == nil {
			t.Errorf("%s: expected validation error", name)
		}
	}
}

func TestValidConfigPasses(t *testing.T) {
	path := writeTemp(t, `
builder: {container: c, workdir: /w, command: [make]}
artifact: {patterns: [x]}
upgrade: {transfer: custom, custom_transfer_command: ["scp {artifact} {dut_address}:{remote_path}"]}
`)
	if _, err := Load(path); err != nil {
		t.Errorf("valid custom-transfer config rejected: %v", err)
	}
}

func TestCustomTransferUnknownPlaceholder(t *testing.T) {
	path := writeTemp(t, `
builder: {container: c, workdir: /w, command: [make]}
artifact: {patterns: [x]}
upgrade: {transfer: custom, custom_transfer_command: ["cp {bogus} /tmp"]}
`)
	if _, err := Load(path); err == nil {
		t.Error("unknown custom-transfer placeholder should be rejected")
	}
}

func TestRedactedDump(t *testing.T) {
	path := writeTemp(t, `
builder:
  container: c
  workdir: /w
  command: [make]
  env:
    API_TOKEN: supersecret
    NORMAL: ok
artifact: {patterns: [x]}
dut:
  login:
    password: hunter2
`)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	dump, err := cfg.RedactedDump()
	if err != nil {
		t.Fatalf("RedactedDump: %v", err)
	}
	dut := dump["dut"].(map[string]any)
	login := dut["login"].(map[string]any)
	if login["password"] != "<redacted>" {
		t.Errorf("password not redacted: %v", login["password"])
	}
	env := dump["builder"].(map[string]any)["env"].(map[string]any)
	if env["API_TOKEN"] != "<redacted>" {
		t.Errorf("sensitive env not redacted: %v", env["API_TOKEN"])
	}
	if env["NORMAL"] != "ok" {
		t.Errorf("non-sensitive env should be intact: %v", env["NORMAL"])
	}
}

func TestStatePathsResolveRelativeToConfig(t *testing.T) {
	cfg, err := Load(filepath.Join("..", "..", "configs", "example.yaml"))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	root := cfg.ArtifactRoot(filepath.Join("..", "..", "configs", "example.yaml"))
	if !filepath.IsAbs(root) {
		t.Errorf("artifact root should be absolute: %q", root)
	}
	db := cfg.StateDBPath(filepath.Join("..", "..", "configs", "example.yaml"))
	if filepath.Base(db) != "owrt_monitor.sqlite3" {
		t.Errorf("state db = %q", db)
	}
}
