// Package config is the Go port of python/owrt_monitor/config.py. It parses the
// same YAML, applies the same defaults, performs ${VAR}/${VAR:-default}
// interpolation, rejects unknown keys (pydantic extra="forbid"), supports the
// profiles deep-merge overlay, and redacts secrets — so both engines accept and
// snapshot the same config files.
package config

import (
	"fmt"

	"gopkg.in/yaml.v3"
)

// ProjectConfig mirrors config.py ProjectConfig.
type ProjectConfig struct {
	Name           string  `yaml:"name" json:"name"`
	ArtifactDir    string  `yaml:"artifact_dir" json:"artifact_dir"`
	StateDB        *string `yaml:"state_db" json:"state_db"`
	DefaultProfile *string `yaml:"default_profile" json:"default_profile"`
}

// BuilderConfig mirrors config.py BuilderConfig.
type BuilderConfig struct {
	Container      string            `yaml:"container" json:"container"`
	Workdir        string            `yaml:"workdir" json:"workdir"`
	Command        []string          `yaml:"command" json:"command"`
	Env            map[string]string `yaml:"env" json:"env"`
	TimeoutSec     int               `yaml:"timeout_sec" json:"timeout_sec"`
	MinFreeDiskMB  int               `yaml:"min_free_disk_mb" json:"min_free_disk_mb"`
	LockTimeoutSec int               `yaml:"lock_timeout_sec" json:"lock_timeout_sec"`
	RequiredPaths  []string          `yaml:"required_paths" json:"required_paths"`
	// Cross-profile contamination guard (see config.py BuilderConfig). The Go
	// engine accepts and round-trips these for config parity; the build-time
	// reaction lives in the Python workflow.
	OnProfileSwitch      string     `yaml:"on_profile_switch" json:"on_profile_switch"`
	ProfileSwitchCleanup [][]string `yaml:"profile_switch_cleanup" json:"profile_switch_cleanup"`
}

// ArtifactConfig mirrors config.py ArtifactConfig.
type ArtifactConfig struct {
	Patterns       []string `yaml:"patterns" json:"patterns"`
	RegexPatterns  []string `yaml:"regex_patterns" json:"regex_patterns"`
	Selection      string   `yaml:"selection" json:"selection"`
	MinSizeMB      float64  `yaml:"min_size_mb" json:"min_size_mb"`
	RequireSHA256  bool     `yaml:"require_sha256" json:"require_sha256"`
	ExportFilename *string  `yaml:"export_filename" json:"export_filename"`
}

// LoginConfig mirrors config.py LoginConfig.
type LoginConfig struct {
	Username string  `yaml:"username" json:"username"`
	Password *string `yaml:"password" json:"password"`
}

// DutNetworkConfig mirrors config.py DutNetworkConfig.
type DutNetworkConfig struct {
	Address   *string `yaml:"address" json:"address"`
	Interface *string `yaml:"interface" json:"interface"`
}

// DutConfig mirrors config.py DutConfig.
type DutConfig struct {
	Name                    string           `yaml:"name" json:"name"`
	Serial                  *string          `yaml:"serial" json:"serial"`
	Baud                    int              `yaml:"baud" json:"baud"`
	Bytesize                int              `yaml:"bytesize" json:"bytesize"`
	Parity                  string           `yaml:"parity" json:"parity"`
	Stopbits                int              `yaml:"stopbits" json:"stopbits"`
	Prompt                  string           `yaml:"prompt" json:"prompt"`
	Newline                 string           `yaml:"newline" json:"newline"`
	ConnectTimeoutSec       int              `yaml:"connect_timeout_sec" json:"connect_timeout_sec"`
	CommandTimeoutSec       int              `yaml:"command_timeout_sec" json:"command_timeout_sec"`
	LockTimeoutSec          int              `yaml:"lock_timeout_sec" json:"lock_timeout_sec"`
	ExpectedArtifactPattern *string          `yaml:"expected_artifact_pattern" json:"expected_artifact_pattern"`
	DiscoveryPatterns       []string         `yaml:"discovery_patterns" json:"discovery_patterns"`
	Login                   LoginConfig      `yaml:"login" json:"login"`
	Network                 DutNetworkConfig `yaml:"network" json:"network"`
}

// BootloaderConfig mirrors config.py BootloaderConfig.
type BootloaderConfig struct {
	Prompt                  string  `yaml:"prompt" json:"prompt"`
	InterruptBanner         string  `yaml:"interrupt_banner" json:"interrupt_banner"`
	InterruptKey            string  `yaml:"interrupt_key" json:"interrupt_key"`
	RestartCommand          string  `yaml:"restart_command" json:"restart_command"`
	ServerIPEnv             string  `yaml:"server_ip_env" json:"server_ip_env"`
	ClientIPEnv             string  `yaml:"client_ip_env" json:"client_ip_env"`
	LoadAddress             string  `yaml:"load_address" json:"load_address"`
	BootCommand             string  `yaml:"boot_command" json:"boot_command"`
	TFTPFilename            *string `yaml:"tftp_filename" json:"tftp_filename"`
	AutobootWaitSec         int     `yaml:"autoboot_wait_sec" json:"autoboot_wait_sec"`
	BootloaderPromptWaitSec int     `yaml:"bootloader_prompt_wait_sec" json:"bootloader_prompt_wait_sec"`
	TFTPLoadWaitSec         int     `yaml:"tftp_load_wait_sec" json:"tftp_load_wait_sec"`
}

// TransferNetworkRecoveryConfig mirrors config.py TransferNetworkRecoveryConfig.
type TransferNetworkRecoveryConfig struct {
	Enabled              bool    `yaml:"enabled" json:"enabled"`
	PingHost             *string `yaml:"ping_host" json:"ping_host"`
	Interface            *string `yaml:"interface" json:"interface"`
	StaticCIDR           string  `yaml:"static_cidr" json:"static_cidr"`
	RestoreAfterTransfer bool    `yaml:"restore_after_transfer" json:"restore_after_transfer"`
}

// PostUpgradeNetworkConfig mirrors config.py PostUpgradeNetworkConfig.
type PostUpgradeNetworkConfig struct {
	EnsureDHCP bool    `yaml:"ensure_dhcp" json:"ensure_dhcp"`
	Interface  *string `yaml:"interface" json:"interface"`
}

// UpgradeConfig mirrors config.py UpgradeConfig.
type UpgradeConfig struct {
	Transfer              string                        `yaml:"transfer" json:"transfer"`
	RemotePath            string                        `yaml:"remote_path" json:"remote_path"`
	Command               string                        `yaml:"command" json:"command"`
	BootTimeoutSec        int                           `yaml:"boot_timeout_sec" json:"boot_timeout_sec"`
	TransferTimeoutSec    int                           `yaml:"transfer_timeout_sec" json:"transfer_timeout_sec"`
	HostInterface         *string                       `yaml:"host_interface" json:"host_interface"`
	HTTPBind              string                        `yaml:"http_bind" json:"http_bind"`
	HTTPHost              *string                       `yaml:"http_host" json:"http_host"`
	HTTPPort              int                           `yaml:"http_port" json:"http_port"`
	TFTPRoot              string                        `yaml:"tftp_root" json:"tftp_root"`
	TFTPHost              *string                       `yaml:"tftp_host" json:"tftp_host"`
	TFTPPort              int                           `yaml:"tftp_port" json:"tftp_port"`
	SCPBinary             string                        `yaml:"scp_binary" json:"scp_binary"`
	SCPUser               string                        `yaml:"scp_user" json:"scp_user"`
	SCPHost               *string                       `yaml:"scp_host" json:"scp_host"`
	SCPPort               int                           `yaml:"scp_port" json:"scp_port"`
	SCPIdentityFile       *string                       `yaml:"scp_identity_file" json:"scp_identity_file"`
	SCPExtraArgs          []string                      `yaml:"scp_extra_args" json:"scp_extra_args"`
	CustomTransferCommand []string                      `yaml:"custom_transfer_command" json:"custom_transfer_command"`
	VerifySHA256          bool                          `yaml:"verify_sha256" json:"verify_sha256"`
	MinDutFreeKB          int                           `yaml:"min_dut_free_kb" json:"min_dut_free_kb"`
	Bootloader            BootloaderConfig              `yaml:"bootloader" json:"bootloader"`
	NetworkRecovery       TransferNetworkRecoveryConfig `yaml:"network_recovery" json:"network_recovery"`
	PostUpgradeNetwork    PostUpgradeNetworkConfig      `yaml:"post_upgrade_network" json:"post_upgrade_network"`
	ConfirmBeforeFlash    bool                          `yaml:"confirm_before_flash" json:"confirm_before_flash"`
	BootFailurePatterns   []string                      `yaml:"boot_failure_patterns" json:"boot_failure_patterns"`
	ExpectedBootMarkers   []string                      `yaml:"expected_boot_markers" json:"expected_boot_markers"`
}

// SmokeTest mirrors config.py SmokeTest. A bare YAML string is accepted as a
// command with no expect (backwards-compat with TestConfig.normalize_smoke).
type SmokeTest struct {
	Command string  `yaml:"command" json:"command"`
	Expect  *string `yaml:"expect" json:"expect"`
	Enabled bool    `yaml:"enabled" json:"enabled"`
}

// ScriptTest mirrors config.py ScriptTest.
type ScriptTest struct {
	Name       string            `yaml:"name" json:"name"`
	Path       string            `yaml:"path" json:"path"`
	Args       []string          `yaml:"args" json:"args"`
	Env        map[string]string `yaml:"env" json:"env"`
	TimeoutSec int               `yaml:"timeout_sec" json:"timeout_sec"`
	Enabled    bool              `yaml:"enabled" json:"enabled"`
}

// PytestTest mirrors config.py PytestTest.
type PytestTest struct {
	Name       string            `yaml:"name" json:"name"`
	Path       string            `yaml:"path" json:"path"`
	Args       []string          `yaml:"args" json:"args"`
	Env        map[string]string `yaml:"env" json:"env"`
	TimeoutSec int               `yaml:"timeout_sec" json:"timeout_sec"`
	Python     *string           `yaml:"python" json:"python"`
	Enabled    bool              `yaml:"enabled" json:"enabled"`
}

// SshTest mirrors config.py SshTest.
type SshTest struct {
	Name         string   `yaml:"name" json:"name"`
	Command      string   `yaml:"command" json:"command"`
	Expect       *string  `yaml:"expect" json:"expect"`
	Host         *string  `yaml:"host" json:"host"`
	User         string   `yaml:"user" json:"user"`
	Port         int      `yaml:"port" json:"port"`
	IdentityFile *string  `yaml:"identity_file" json:"identity_file"`
	SSHBinary    string   `yaml:"ssh_binary" json:"ssh_binary"`
	ExtraArgs    []string `yaml:"extra_args" json:"extra_args"`
	TimeoutSec   int      `yaml:"timeout_sec" json:"timeout_sec"`
	Enabled      bool     `yaml:"enabled" json:"enabled"`
}

// TestConfig mirrors config.py TestConfig.
type TestConfig struct {
	Smoke             []SmokeTest  `yaml:"smoke" json:"smoke"`
	Scripts           []ScriptTest `yaml:"scripts" json:"scripts"`
	Pytest            []PytestTest `yaml:"pytest" json:"pytest"`
	SSH               []SshTest    `yaml:"ssh" json:"ssh"`
	CommandTimeoutSec int          `yaml:"command_timeout_sec" json:"command_timeout_sec"`
	StatusCommand     string       `yaml:"status_command" json:"status_command"`
}

// RetryPolicy mirrors config.py RetryPolicy.
type RetryPolicy struct {
	Attempts   int     `yaml:"attempts" json:"attempts"`
	BackoffSec float64 `yaml:"backoff_sec" json:"backoff_sec"`
}

// RetryConfig mirrors config.py RetryConfig.
type RetryConfig struct {
	ArtifactSelect   RetryPolicy `yaml:"artifact_select" json:"artifact_select"`
	ArtifactExport   RetryPolicy `yaml:"artifact_export" json:"artifact_export"`
	FirmwareTransfer RetryPolicy `yaml:"firmware_transfer" json:"firmware_transfer"`
	SmokeTests       RetryPolicy `yaml:"smoke_tests" json:"smoke_tests"`
}

// OwrtConfig mirrors config.py OwrtConfig (the top-level document).
type OwrtConfig struct {
	Project  ProjectConfig             `yaml:"project" json:"project"`
	Builder  BuilderConfig             `yaml:"builder" json:"builder"`
	Artifact ArtifactConfig            `yaml:"artifact" json:"artifact"`
	Dut      DutConfig                 `yaml:"dut" json:"dut"`
	Upgrade  UpgradeConfig             `yaml:"upgrade" json:"upgrade"`
	Tests    TestConfig                `yaml:"tests" json:"tests"`
	Retry    RetryConfig               `yaml:"retry" json:"retry"`
	Profiles map[string]map[string]any `yaml:"profiles" json:"profiles"`
}

// Defaults returns an OwrtConfig pre-populated with config.py's field defaults.
// Decoding YAML onto this value leaves omitted keys at their defaults, matching
// pydantic's default/default_factory behavior.
func Defaults() OwrtConfig {
	return OwrtConfig{
		Project: ProjectConfig{Name: "owrt-monitor-lab", ArtifactDir: "artifacts"},
		Builder: BuilderConfig{
			Env: map[string]string{}, MinFreeDiskMB: 5000, LockTimeoutSec: 3600,
			RequiredPaths: []string{}, OnProfileSwitch: "warn",
			ProfileSwitchCleanup: [][]string{},
		},
		Artifact: ArtifactConfig{
			RegexPatterns: []string{}, Selection: "newest", MinSizeMB: 0, RequireSHA256: true,
		},
		Dut:     defaultDut(),
		Upgrade: defaultUpgrade(),
		Tests: TestConfig{
			Smoke: []SmokeTest{}, Scripts: []ScriptTest{}, Pytest: []PytestTest{},
			SSH: []SshTest{}, CommandTimeoutSec: 30, StatusCommand: "ubus call system board",
		},
		Retry: RetryConfig{
			ArtifactSelect:   RetryPolicy{Attempts: 1},
			ArtifactExport:   RetryPolicy{Attempts: 1},
			FirmwareTransfer: RetryPolicy{Attempts: 1},
			SmokeTests:       RetryPolicy{Attempts: 1},
		},
		Profiles: map[string]map[string]any{},
	}
}

func defaultDut() DutConfig {
	return DutConfig{
		Name: "dut-01", Baud: 115200, Bytesize: 8, Parity: "none", Stopbits: 1,
		Prompt: `root@OpenWrt:.*# `, Newline: "lf",
		ConnectTimeoutSec: 30, CommandTimeoutSec: 30, LockTimeoutSec: 1800,
		DiscoveryPatterns: []string{
			"/dev/cu.usbserial-*", "/dev/tty.usbserial-*",
			"/dev/cu.usbmodem*", "/dev/tty.usbmodem*",
		},
		Login:   LoginConfig{Username: "root"},
		Network: DutNetworkConfig{},
	}
}

func defaultBootloader() BootloaderConfig {
	return BootloaderConfig{
		Prompt: "=> ", InterruptBanner: "Hit any key to stop autoboot", InterruptKey: " ",
		RestartCommand: "reboot", ServerIPEnv: "serverip", ClientIPEnv: "ipaddr",
		LoadAddress: "0x80000000", BootCommand: "bootm",
		AutobootWaitSec: 60, BootloaderPromptWaitSec: 10, TFTPLoadWaitSec: 120,
	}
}

func defaultUpgrade() UpgradeConfig {
	return UpgradeConfig{
		Transfer: "http", RemotePath: "/tmp/firmware.bin",
		Command: "sysupgrade -n /tmp/firmware.bin", BootTimeoutSec: 240, TransferTimeoutSec: 180,
		HTTPBind: "0.0.0.0", HTTPPort: 0, TFTPRoot: "/private/tftpboot", TFTPPort: 0,
		SCPBinary: "scp", SCPUser: "root", SCPPort: 22,
		SCPExtraArgs: []string{}, CustomTransferCommand: []string{}, VerifySHA256: true,
		Bootloader:         defaultBootloader(),
		NetworkRecovery:    TransferNetworkRecoveryConfig{StaticCIDR: "192.168.1.1/24", RestoreAfterTransfer: true},
		PostUpgradeNetwork: PostUpgradeNetworkConfig{},
		BootFailurePatterns: []string{
			`Kernel panic - not syncing`,
			`^Oops: `,
			`\[<[0-9a-fA-F]+>\] panic\+`,
			`Unable to handle kernel paging request`,
		},
		ExpectedBootMarkers: []string{},
	}
}

// UnmarshalYAML accepts either a bare command string or a mapping for a smoke
// test, defaulting Enabled to true (config.py SmokeTest.enabled default).
func (s *SmokeTest) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind == yaml.ScalarNode {
		s.Command = node.Value
		s.Enabled = true
		return nil
	}
	if err := checkKnownKeys(node, smokeKeys, "tests.smoke[]"); err != nil {
		return err
	}
	type raw SmokeTest
	tmp := raw{Enabled: true}
	if err := node.Decode(&tmp); err != nil {
		return err
	}
	*s = SmokeTest(tmp)
	return nil
}

func (t *ScriptTest) UnmarshalYAML(node *yaml.Node) error {
	if err := checkKnownKeys(node, scriptKeys, "tests.scripts[]"); err != nil {
		return err
	}
	type raw ScriptTest
	tmp := raw{Args: []string{}, Env: map[string]string{}, TimeoutSec: 60, Enabled: true}
	if err := node.Decode(&tmp); err != nil {
		return err
	}
	*t = ScriptTest(tmp)
	return nil
}

func (t *PytestTest) UnmarshalYAML(node *yaml.Node) error {
	if err := checkKnownKeys(node, pytestKeys, "tests.pytest[]"); err != nil {
		return err
	}
	type raw PytestTest
	tmp := raw{Args: []string{}, Env: map[string]string{}, TimeoutSec: 300, Enabled: true}
	if err := node.Decode(&tmp); err != nil {
		return err
	}
	*t = PytestTest(tmp)
	return nil
}

func (t *SshTest) UnmarshalYAML(node *yaml.Node) error {
	if err := checkKnownKeys(node, sshKeys, "tests.ssh[]"); err != nil {
		return err
	}
	type raw SshTest
	tmp := raw{User: "root", Port: 22, SSHBinary: "ssh", ExtraArgs: []string{}, TimeoutSec: 30, Enabled: true}
	if err := node.Decode(&tmp); err != nil {
		return err
	}
	*t = SshTest(tmp)
	return nil
}

var (
	smokeKeys  = keySet("command", "expect", "enabled")
	scriptKeys = keySet("name", "path", "args", "env", "timeout_sec", "enabled")
	pytestKeys = keySet("name", "path", "args", "env", "timeout_sec", "python", "enabled")
	sshKeys    = keySet("name", "command", "expect", "host", "user", "port",
		"identity_file", "ssh_binary", "extra_args", "timeout_sec", "enabled")
)

func keySet(keys ...string) map[string]bool {
	out := make(map[string]bool, len(keys))
	for _, k := range keys {
		out[k] = true
	}
	return out
}

// checkKnownKeys enforces extra="forbid" for list-element types, which
// yaml.Node.Decode does not check on its own.
func checkKnownKeys(node *yaml.Node, allowed map[string]bool, ctx string) error {
	if node.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(node.Content); i += 2 {
		key := node.Content[i].Value
		if !allowed[key] {
			return fmt.Errorf("%s: unknown field %q", ctx, key)
		}
	}
	return nil
}
