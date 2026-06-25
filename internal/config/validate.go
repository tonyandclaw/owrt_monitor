package config

import (
	"fmt"
	"regexp"
	"strings"
)

// Validate enforces the field constraints from config.py's pydantic validators.
// It fails fast with a descriptive message. Note: Go's regexp is RE2, so a few
// exotic Python regexes (backreferences, lookarounds) that config.py would
// accept are rejected here; the shipped defaults are all RE2-compatible.
func (c *OwrtConfig) Validate() error {
	if err := c.Builder.validate(); err != nil {
		return err
	}
	if err := c.Artifact.validate(); err != nil {
		return err
	}
	if err := c.Dut.validate(); err != nil {
		return err
	}
	if err := c.Upgrade.validate(); err != nil {
		return err
	}
	if err := c.Tests.validate(); err != nil {
		return err
	}
	if err := c.Retry.validate(); err != nil {
		return err
	}
	if c.Project.DefaultProfile != nil {
		if strings.TrimSpace(*c.Project.DefaultProfile) == "" {
			return fmt.Errorf("project.default_profile must not be blank")
		}
		if _, ok := c.Profiles[*c.Project.DefaultProfile]; !ok {
			avail := strings.Join(c.ListProfiles(), ", ")
			if avail == "" {
				avail = "(no profiles defined)"
			}
			return fmt.Errorf(
				"project.default_profile %q is not defined in profiles; available: %s",
				*c.Project.DefaultProfile, avail)
		}
	}
	return nil
}

func (b *BuilderConfig) validate() error {
	if strings.TrimSpace(b.Container) == "" {
		return fmt.Errorf("builder.container is required")
	}
	if strings.TrimSpace(b.Workdir) == "" {
		return fmt.Errorf("builder.workdir is required")
	}
	if len(b.Command) == 0 {
		return fmt.Errorf("builder.command must contain at least one argument")
	}
	if b.TimeoutSec < 0 {
		return fmt.Errorf("builder.timeout_sec must be 0 or greater")
	}
	if b.MinFreeDiskMB < 0 {
		return fmt.Errorf("builder.min_free_disk_mb must be 0 or greater (0 disables the check)")
	}
	if b.LockTimeoutSec <= 0 {
		return fmt.Errorf("builder.lock_timeout_sec must be positive")
	}
	switch b.OnProfileSwitch {
	case "", "off", "warn", "clean":
	default:
		return fmt.Errorf("builder.on_profile_switch must be one of [clean off warn], got %q", b.OnProfileSwitch)
	}
	for _, cmd := range b.ProfileSwitchCleanup {
		if len(cmd) == 0 {
			return fmt.Errorf("each builder.profile_switch_cleanup entry must contain at least one argument")
		}
	}
	return nil
}

func (a *ArtifactConfig) validate() error {
	if len(a.Patterns) == 0 {
		return fmt.Errorf("artifact.patterns must contain at least one glob")
	}
	for _, p := range a.RegexPatterns {
		if _, err := regexp.Compile(p); err != nil {
			return fmt.Errorf("artifact.regex_patterns contains invalid regex %q: %v", p, err)
		}
	}
	if !selectionValues[a.Selection] {
		return fmt.Errorf("artifact.selection must be one of newest, largest, fail-if-multiple")
	}
	if a.MinSizeMB < 0 {
		return fmt.Errorf("artifact.min_size_mb must be 0 or greater")
	}
	return nil
}

func (d *DutConfig) validate() error {
	if d.Baud <= 0 {
		return fmt.Errorf("dut.baud must be positive")
	}
	switch d.Bytesize {
	case 5, 6, 7, 8:
	default:
		return fmt.Errorf("dut.bytesize must be one of 5, 6, 7, 8")
	}
	if !parityValues[d.Parity] {
		return fmt.Errorf("dut.parity must be one of none, even, odd, mark, space")
	}
	if d.Stopbits != 1 && d.Stopbits != 2 {
		return fmt.Errorf("dut.stopbits must be 1 or 2")
	}
	if d.Newline != "lf" && d.Newline != "crlf" {
		return fmt.Errorf("dut.newline must be lf or crlf")
	}
	if d.ConnectTimeoutSec <= 0 || d.CommandTimeoutSec <= 0 {
		return fmt.Errorf("DUT timeouts must be positive")
	}
	if d.LockTimeoutSec <= 0 {
		return fmt.Errorf("dut.lock_timeout_sec must be positive")
	}
	if d.ExpectedArtifactPattern != nil {
		if _, err := regexp.Compile(*d.ExpectedArtifactPattern); err != nil {
			return fmt.Errorf("dut.expected_artifact_pattern is not a valid regex: %v", err)
		}
	}
	return nil
}

func (u *UpgradeConfig) validate() error {
	if !transferValues[u.Transfer] {
		return fmt.Errorf("upgrade.transfer must be one of http, scp, tftp, bootloader_tftp, custom")
	}
	if u.BootTimeoutSec <= 0 || u.TransferTimeoutSec <= 0 {
		return fmt.Errorf("upgrade timeouts must be positive")
	}
	if u.HTTPPort < 0 || u.HTTPPort > 65535 || u.TFTPPort < 0 || u.TFTPPort > 65535 {
		return fmt.Errorf("upgrade.http_port/upgrade.tftp_port must be between 0 and 65535")
	}
	if u.SCPPort <= 0 || u.SCPPort > 65535 {
		return fmt.Errorf("upgrade.scp_port must be between 1 and 65535")
	}
	if u.MinDutFreeKB < 0 {
		return fmt.Errorf("upgrade.min_dut_free_kb must be 0 or greater (0 disables)")
	}
	if strings.TrimSpace(u.SCPBinary) == "" || strings.TrimSpace(u.SCPUser) == "" {
		return fmt.Errorf("upgrade.scp_binary and upgrade.scp_user must not be blank")
	}
	if u.HostInterface != nil && strings.TrimSpace(*u.HostInterface) == "" {
		return fmt.Errorf("upgrade.host_interface must not be blank")
	}
	for _, e := range u.SCPExtraArgs {
		if strings.TrimSpace(e) == "" {
			return fmt.Errorf("upgrade.scp_extra_args entries must not be blank")
		}
	}
	for _, e := range u.CustomTransferCommand {
		if strings.TrimSpace(e) == "" {
			return fmt.Errorf("upgrade.custom_transfer_command entries must not be blank")
		}
		for _, tok := range placeholderToke.FindAllStringSubmatch(e, -1) {
			if !customTransferPlaceholders[tok[1]] {
				allowed := sortedKeys(customTransferPlaceholders)
				return fmt.Errorf(
					"upgrade.custom_transfer_command contains unknown placeholder {%s}; allowed placeholders: %s",
					tok[1], strings.Join(allowed, ", "))
			}
		}
	}
	if u.Transfer == "custom" && len(u.CustomTransferCommand) == 0 {
		return fmt.Errorf("upgrade.custom_transfer_command is required when upgrade.transfer is custom")
	}
	for _, p := range u.ExpectedBootMarkers {
		if _, err := regexp.Compile(p); err != nil {
			return fmt.Errorf("upgrade.expected_boot_markers contains invalid regex %q: %v", p, err)
		}
	}
	return u.Bootloader.validate()
}

func (b *BootloaderConfig) validate() error {
	if b.AutobootWaitSec <= 0 || b.BootloaderPromptWaitSec <= 0 || b.TFTPLoadWaitSec <= 0 {
		return fmt.Errorf("upgrade.bootloader timeouts must be positive")
	}
	for _, p := range []string{b.Prompt, b.InterruptBanner} {
		if _, err := regexp.Compile(p); err != nil {
			return fmt.Errorf("upgrade.bootloader regex %q: %v", p, err)
		}
	}
	return nil
}

func (t *TestConfig) validate() error {
	if t.CommandTimeoutSec <= 0 {
		return fmt.Errorf("tests.command_timeout_sec must be positive")
	}
	for _, s := range t.Smoke {
		if strings.TrimSpace(s.Command) == "" {
			return fmt.Errorf("tests.smoke[].command must not be blank")
		}
		if s.Expect != nil {
			if _, err := regexp.Compile(*s.Expect); err != nil {
				return fmt.Errorf("tests.smoke[].expect is not a valid regex: %v", err)
			}
		}
	}
	for _, s := range t.Scripts {
		if strings.TrimSpace(s.Name) == "" || strings.TrimSpace(s.Path) == "" {
			return fmt.Errorf("tests.scripts[].name and .path must not be blank")
		}
		if s.TimeoutSec <= 0 {
			return fmt.Errorf("tests.scripts[].timeout_sec must be positive")
		}
	}
	for _, p := range t.Pytest {
		if strings.TrimSpace(p.Name) == "" || strings.TrimSpace(p.Path) == "" {
			return fmt.Errorf("tests.pytest[].name and .path must not be blank")
		}
		if p.Python != nil && strings.TrimSpace(*p.Python) == "" {
			return fmt.Errorf("tests.pytest[].python must not be blank")
		}
		if p.TimeoutSec <= 0 {
			return fmt.Errorf("tests.pytest[].timeout_sec must be positive")
		}
	}
	for _, s := range t.SSH {
		if strings.TrimSpace(s.Name) == "" || strings.TrimSpace(s.Command) == "" ||
			strings.TrimSpace(s.User) == "" || strings.TrimSpace(s.SSHBinary) == "" {
			return fmt.Errorf("tests.ssh[].name, .command, .user, and .ssh_binary must not be blank")
		}
		if s.Expect != nil {
			if _, err := regexp.Compile(*s.Expect); err != nil {
				return fmt.Errorf("tests.ssh[].expect is not a valid regex: %v", err)
			}
		}
		if s.Port <= 0 || s.Port > 65535 {
			return fmt.Errorf("tests.ssh[].port must be between 1 and 65535")
		}
		for _, e := range s.ExtraArgs {
			if strings.TrimSpace(e) == "" {
				return fmt.Errorf("tests.ssh[].extra_args entries must not be blank")
			}
		}
		if s.TimeoutSec <= 0 {
			return fmt.Errorf("tests.ssh[].timeout_sec must be positive")
		}
	}
	return nil
}

func (r *RetryConfig) validate() error {
	for _, p := range []RetryPolicy{r.ArtifactSelect, r.ArtifactExport, r.FirmwareTransfer, r.SmokeTests} {
		if p.Attempts < 1 {
			return fmt.Errorf("retry.attempts must be at least 1")
		}
		if p.BackoffSec < 0 {
			return fmt.Errorf("retry.backoff_sec must be 0 or greater")
		}
	}
	return nil
}

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	// simple insertion sort to avoid importing sort twice; small set
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j] < out[j-1]; j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}
	return out
}
