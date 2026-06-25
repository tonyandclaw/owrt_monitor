from __future__ import annotations

import os
import re
from pathlib import Path
from string import Formatter
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigError(ValueError):
    """Raised when a config file cannot be loaded or validated."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|private[_-]?key)", re.I
)
_CUSTOM_TRANSFER_PLACEHOLDERS = frozenset(
    {
        "artifact",
        "artifact_path",
        "filename",
        "sha256",
        "size_bytes",
        "remote_path",
        "dut_name",
        "dut_serial",
        "dut_address",
        "run_dir",
        "job_id",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectConfig(StrictModel):
    name: str = "owrt-monitor-lab"
    artifact_dir: Path = Path("artifacts")
    state_db: Path | None = None
    # Optional profile applied when a command does not pass `--profile`.
    # This lets a lab config keep a normal default board while preserving
    # explicit overlays for the other boards.
    default_profile: str | None = None

    @field_validator("default_profile")
    @classmethod
    def default_profile_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("project.default_profile must not be blank")
        return value


class BuilderConfig(StrictModel):
    container: str
    workdir: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = 0
    min_free_disk_mb: int = 5000
    lock_timeout_sec: int = 3600
    # Optional list of absolute paths inside the container that must exist
    # before the build runs (e.g. `["feeds.conf", "package/feeds/mtk"]` to
    # confirm the lab's customised feeds are checked out). Each is verified
    # with `docker exec test -e`. Empty list disables the check.
    required_paths: list[str] = Field(default_factory=list)
    # Cross-profile contamination guard. AP/controller/gateway share one build
    # tree (e.g. build/owrt2102), and OpenWrt does not rebuild a package just
    # because the target profile changed — so a package with profile-conditional
    # DEPENDS (e.g. asus-base-files gaining +pgsql-server only on the Controller
    # profile) keeps the *previous* profile's deps and breaks `package/install`.
    # When the last successful build in this same builder targeted a different
    # `command` (board), the workflow reacts per `on_profile_switch`:
    #   "off"   -> do nothing
    #   "warn"  -> log a warning + note it in the report (default)
    #   "clean" -> run each `profile_switch_cleanup` command in the container
    #              (workdir) before building, then build normally
    on_profile_switch: str = "warn"
    # Commands run (as argument arrays, no shell) inside the builder workdir when
    # a profile switch is detected and `on_profile_switch` is "clean". Typically
    # one `make package/<name>/clean` per profile-conditional package, e.g.
    # `[[make, "-C", "build/owrt2102", "package/asus-base-files/clean"]]`.
    profile_switch_cleanup: list[list[str]] = Field(default_factory=list)

    @field_validator("command")
    @classmethod
    def command_must_not_be_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("builder.command must contain at least one argument")
        return value

    @field_validator("timeout_sec")
    @classmethod
    def timeout_must_not_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("builder.timeout_sec must be 0 or greater")
        return value

    @field_validator("min_free_disk_mb")
    @classmethod
    def disk_floor_must_not_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                "builder.min_free_disk_mb must be 0 or greater (0 disables the check)"
            )
        return value

    @field_validator("lock_timeout_sec")
    @classmethod
    def builder_lock_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("builder.lock_timeout_sec must be positive")
        return value

    @field_validator("on_profile_switch")
    @classmethod
    def on_profile_switch_must_be_known(cls, value: str) -> str:
        allowed = {"off", "warn", "clean"}
        if value not in allowed:
            raise ValueError(
                f"builder.on_profile_switch must be one of {sorted(allowed)}, got {value!r}"
            )
        return value

    @field_validator("profile_switch_cleanup")
    @classmethod
    def cleanup_commands_must_not_be_empty(cls, value: list[list[str]]) -> list[list[str]]:
        for command in value:
            if not command:
                raise ValueError(
                    "each builder.profile_switch_cleanup entry must contain at least one argument"
                )
        return value


class ArtifactConfig(StrictModel):
    patterns: list[str]
    # Optional regex filter applied AFTER glob expansion. Each pattern in this
    # list is `re.search`'d against the relative path (matched anywhere in
    # the path). Useful when the glob is too broad to express the desired
    # match — e.g., excluding factory.bin variants while keeping sysupgrade.bin.
    # Empty list means no regex filtering (default).
    regex_patterns: list[str] = Field(default_factory=list)
    selection: Literal["newest", "largest", "fail-if-multiple"] = "newest"
    min_size_mb: float = 0
    require_sha256: bool = True
    export_filename: str | None = None

    @field_validator("patterns")
    @classmethod
    def patterns_must_not_be_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("artifact.patterns must contain at least one glob")
        return value

    @field_validator("regex_patterns")
    @classmethod
    def regex_patterns_must_compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"artifact.regex_patterns contains invalid regex {pattern!r}: {exc}"
                ) from exc
        return value

    @field_validator("min_size_mb")
    @classmethod
    def min_size_must_not_be_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("artifact.min_size_mb must be 0 or greater")
        return value


class LoginConfig(StrictModel):
    username: str = "root"
    password: str | None = None


class DutNetworkConfig(StrictModel):
    address: str | None = None
    interface: str | None = None


class DutConfig(StrictModel):
    name: str = "dut-01"
    serial: str | None = None
    baud: int = 115200
    # Standard serial framing knobs. Defaults are 8-N-1, which matches every
    # OpenWrt board we've seen. Override per-profile when a board needs odd/even
    # parity or 7 data bits.
    bytesize: Literal[5, 6, 7, 8] = 8
    parity: Literal["none", "even", "odd", "mark", "space"] = "none"
    stopbits: Literal[1, 2] = 1  # half-bit (1.5) is uncommon; not exposed
    prompt: str = r"root@OpenWrt:.*# "
    newline: Literal["lf", "crlf"] = "lf"
    connect_timeout_sec: int = 30
    command_timeout_sec: int = 30
    lock_timeout_sec: int = 1800
    # Optional regex (re.search) the selected artifact's filename must match
    # before flashing. Catches "wrong variant for this board" mistakes — e.g.
    # AP and controller profiles share build/owrt2102/, so a too-broad
    # artifact glob could accidentally pick the wrong board's image.
    expected_artifact_pattern: str | None = None
    discovery_patterns: list[str] = Field(
        default_factory=lambda: [
            "/dev/cu.usbserial-*",
            "/dev/tty.usbserial-*",
            "/dev/cu.usbmodem*",
            "/dev/tty.usbmodem*",
        ]
    )
    login: LoginConfig = Field(default_factory=LoginConfig)
    network: DutNetworkConfig = Field(default_factory=DutNetworkConfig)

    @field_validator("baud")
    @classmethod
    def baud_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("dut.baud must be positive")
        return value

    @field_validator("connect_timeout_sec", "command_timeout_sec")
    @classmethod
    def dut_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("DUT timeouts must be positive")
        return value

    @field_validator("lock_timeout_sec")
    @classmethod
    def lock_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("dut.lock_timeout_sec must be positive")
        return value

    @field_validator("expected_artifact_pattern")
    @classmethod
    def expected_pattern_must_compile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(
                f"dut.expected_artifact_pattern is not a valid regex: {exc}"
            ) from exc
        return value


class BootloaderConfig(StrictModel):
    """Settings for the `bootloader_tftp` upgrade flow: drop into U-Boot
    (or similar), TFTP-load the firmware, run it.

    Defaults target standard U-Boot on MT79xx-class hardware. Override per
    profile when the actual board differs.
    """

    prompt: str = r"=> "
    # Banner that signals we're in the autoboot countdown. As soon as we see
    # this, we send `interrupt_key` repeatedly until `prompt` shows up.
    interrupt_banner: str = r"Hit any key to stop autoboot"
    interrupt_key: str = " "
    # Shell command to send to the running OpenWrt to trigger the reboot.
    # Some boards prefer `reset` over `reboot`.
    restart_command: str = "reboot"
    # U-Boot env names — almost universally `serverip` / `ipaddr`.
    server_ip_env: str = "serverip"
    client_ip_env: str = "ipaddr"
    # Where in RAM the image lands. Standard ARM RAM start for MT79xx.
    load_address: str = "0x80000000"
    # What to run after `tftpboot` succeeds. `bootm` boots the loaded image
    # without writing to flash (volatile — lost on next power cycle).
    boot_command: str = "bootm"
    # Optional override for the firmware filename served by tftpd. Defaults
    # to the artifact's filename when None.
    tftp_filename: str | None = None
    # Max time to wait between rebooting and seeing the autoboot banner.
    autoboot_wait_sec: int = 60
    # Max time to wait for the bootloader prompt after sending interrupt key.
    bootloader_prompt_wait_sec: int = 10
    # Max time to wait for `tftpboot` to finish (large images on slow nets).
    tftp_load_wait_sec: int = 120

    @field_validator(
        "autoboot_wait_sec",
        "bootloader_prompt_wait_sec",
        "tftp_load_wait_sec",
    )
    @classmethod
    def waits_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("upgrade.bootloader timeouts must be positive")
        return value

    @field_validator("prompt", "interrupt_banner")
    @classmethod
    def regex_must_compile(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"upgrade.bootloader regex {value!r}: {exc}") from exc
        return value


class TransferNetworkRecoveryConfig(StrictModel):
    # Runtime-only rescue for DUT images whose transfer interface is configured
    # as DHCP but no lease is available yet. The workflow never writes UCI.
    enabled: bool = False
    ping_host: str | None = None
    interface: str | None = None
    static_cidr: str = "192.168.1.1/24"
    restore_after_transfer: bool = True

    @field_validator("ping_host", "interface")
    @classmethod
    def optional_strings_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("upgrade.network_recovery strings must not be blank")
        return value

    @field_validator("static_cidr")
    @classmethod
    def static_cidr_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("upgrade.network_recovery.static_cidr must not be blank")
        return value


class PostUpgradeNetworkConfig(StrictModel):
    # Optional post-boot normalization after sysupgrade/bootloader boot. This is
    # intentionally separate from transfer network recovery, which is runtime-only
    # and happens before flashing.
    ensure_dhcp: bool = False
    interface: str | None = None

    @field_validator("interface")
    @classmethod
    def optional_interface_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("upgrade.post_upgrade_network.interface must not be blank")
        return value


class UpgradeConfig(StrictModel):
    transfer: Literal["http", "scp", "tftp", "bootloader_tftp", "custom"] = "http"
    remote_path: str = "/tmp/firmware.bin"
    command: str = "sysupgrade -n /tmp/firmware.bin"
    boot_timeout_sec: int = 240
    transfer_timeout_sec: int = 180
    host_interface: str | None = None
    http_bind: str = "0.0.0.0"
    http_host: str | None = None
    http_port: int = 0
    tftp_root: str = "/private/tftpboot"
    tftp_host: str | None = None
    tftp_port: int = 0
    scp_binary: str = "scp"
    scp_user: str = "root"
    scp_host: str | None = None
    scp_port: int = 22
    scp_identity_file: Path | None = None
    scp_extra_args: list[str] = Field(default_factory=list)
    custom_transfer_command: list[str] = Field(default_factory=list)
    verify_sha256: bool = True
    min_dut_free_kb: int = 0  # 0 disables; set to e.g. 32768 (32 MB) to require headroom
    bootloader: BootloaderConfig = Field(default_factory=BootloaderConfig)
    network_recovery: TransferNetworkRecoveryConfig = Field(
        default_factory=TransferNetworkRecoveryConfig
    )
    post_upgrade_network: PostUpgradeNetworkConfig = Field(
        default_factory=PostUpgradeNetworkConfig
    )
    # Interactive `[y/N]` prompt right before the destructive command runs.
    # Off by default. When on, reads from stdin via input(); silently skipped
    # if stdin is not a TTY (CI, background scripts), preserving automation.
    confirm_before_flash: bool = False
    boot_failure_patterns: list[str] = Field(
        default_factory=lambda: [
            r"Kernel panic - not syncing",
            r"^Oops: ",
            r"\[<[0-9a-fA-F]+>\] panic\+",
            r"Unable to handle kernel paging request",
        ]
    )
    # Positive-signal regexes; ALL must appear in the boot transcript before
    # the shell prompt for the boot to be considered healthy. Empty list (the
    # default) means "any prompt is enough" — preserves the legacy behavior.
    # Useful values for OpenWrt: ["BusyBox v", "OpenWrt"]; for board-specific
    # confirmation: a model name from the boot banner.
    expected_boot_markers: list[str] = Field(default_factory=list)

    @field_validator("boot_timeout_sec", "transfer_timeout_sec")
    @classmethod
    def upgrade_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("upgrade timeouts must be positive")
        return value

    @field_validator("http_port", "tftp_port")
    @classmethod
    def dynamic_port_must_be_valid(cls, value: int) -> int:
        if value < 0 or value > 65535:
            raise ValueError("upgrade.http_port/upgrade.tftp_port must be between 0 and 65535")
        return value

    @field_validator("scp_port")
    @classmethod
    def scp_port_must_be_valid(cls, value: int) -> int:
        if value <= 0 or value > 65535:
            raise ValueError("upgrade.scp_port must be between 1 and 65535")
        return value

    @field_validator("min_dut_free_kb")
    @classmethod
    def min_dut_free_must_not_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("upgrade.min_dut_free_kb must be 0 or greater (0 disables)")
        return value

    @field_validator("custom_transfer_command")
    @classmethod
    def custom_transfer_command_entries_must_not_be_blank(
        cls, value: list[str]
    ) -> list[str]:
        for entry in value:
            if not entry.strip():
                raise ValueError("upgrade.custom_transfer_command entries must not be blank")
            try:
                parsed = Formatter().parse(entry)
                for _, field_name, _, _ in parsed:
                    if field_name is None:
                        continue
                    if field_name not in _CUSTOM_TRANSFER_PLACEHOLDERS:
                        allowed = ", ".join(sorted(_CUSTOM_TRANSFER_PLACEHOLDERS))
                        raise ValueError(
                            "upgrade.custom_transfer_command contains unknown "
                            f"placeholder {{{field_name}}}; allowed placeholders: {allowed}"
                        )
            except ValueError as exc:
                if "unknown placeholder" in str(exc):
                    raise
                raise ValueError(
                    "upgrade.custom_transfer_command contains invalid placeholder "
                    f"syntax in {entry!r}: {exc}"
                ) from exc
        return value

    @field_validator("scp_binary", "scp_user")
    @classmethod
    def scp_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("upgrade.scp_binary and upgrade.scp_user must not be blank")
        return value

    @field_validator("host_interface")
    @classmethod
    def host_interface_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("upgrade.host_interface must not be blank")
        return value

    @field_validator("scp_extra_args")
    @classmethod
    def scp_extra_args_must_not_be_blank(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not entry.strip():
                raise ValueError("upgrade.scp_extra_args entries must not be blank")
        return value

    @model_validator(mode="after")
    def custom_transfer_requires_command(self) -> UpgradeConfig:
        if self.transfer == "custom" and not self.custom_transfer_command:
            raise ValueError(
                "upgrade.custom_transfer_command is required when upgrade.transfer is custom"
            )
        return self

    @field_validator("expected_boot_markers")
    @classmethod
    def boot_markers_must_compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"upgrade.expected_boot_markers contains invalid regex {pattern!r}: {exc}"
                ) from exc
        return value


class SmokeTest(StrictModel):
    """One smoke-test entry. `expect` (regex) makes the test fail unless the
    command output matches; without `expect` the test passes whenever the
    command itself returns successfully (legacy behavior)."""

    command: str
    expect: str | None = None
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def command_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tests.smoke[].command must not be blank")
        return value

    @field_validator("expect")
    @classmethod
    def expect_must_compile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"tests.smoke[].expect is not a valid regex: {exc}") from exc
        return value


class ScriptTest(StrictModel):
    """A host-side executable run after serial smoke tests.

    The script is exec'd directly (argument-list form, no shell). DUT context
    is exposed via env vars: `OWRT_DUT_NAME`, `OWRT_DUT_SERIAL`,
    `OWRT_DUT_ADDRESS`, `OWRT_RUN_DIR`, `OWRT_FIRMWARE_PATH` (if available),
    `OWRT_FIRMWARE_SHA256` (if available). Exit code 0 = pass.
    """

    name: str
    path: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = 60
    enabled: bool = True

    @field_validator("name", "path")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tests.scripts[].name and .path must not be blank")
        return value

    @field_validator("timeout_sec")
    @classmethod
    def timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("tests.scripts[].timeout_sec must be positive")
        return value


class PytestTest(StrictModel):
    """A host-side pytest invocation run after serial smoke tests.

    Uses `python -m pytest` by default so it runs in the same interpreter as
    owrt-monitor. DUT context is exposed through the same `OWRT_*` environment
    variables as `tests.scripts[]`.
    """

    name: str
    path: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = 300
    python: str | None = None
    enabled: bool = True

    @field_validator("name", "path")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tests.pytest[].name and .path must not be blank")
        return value

    @field_validator("python")
    @classmethod
    def python_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("tests.pytest[].python must not be blank")
        return value

    @field_validator("timeout_sec")
    @classmethod
    def timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("tests.pytest[].timeout_sec must be positive")
        return value


class SshTest(StrictModel):
    """One SSH-based post-upgrade test command."""

    name: str
    command: str
    expect: str | None = None
    host: str | None = None
    user: str = "root"
    port: int = 22
    identity_file: Path | None = None
    ssh_binary: str = "ssh"
    extra_args: list[str] = Field(default_factory=list)
    timeout_sec: int = 30
    enabled: bool = True

    @field_validator("name", "command", "user", "ssh_binary")
    @classmethod
    def strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tests.ssh[].name, .command, .user, and .ssh_binary must not be blank")
        return value

    @field_validator("expect")
    @classmethod
    def expect_must_compile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"tests.ssh[].expect is not a valid regex: {exc}") from exc
        return value

    @field_validator("port")
    @classmethod
    def port_must_be_valid(cls, value: int) -> int:
        if value <= 0 or value > 65535:
            raise ValueError("tests.ssh[].port must be between 1 and 65535")
        return value

    @field_validator("extra_args")
    @classmethod
    def extra_args_must_not_be_blank(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not entry.strip():
                raise ValueError("tests.ssh[].extra_args entries must not be blank")
        return value

    @field_validator("timeout_sec")
    @classmethod
    def timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("tests.ssh[].timeout_sec must be positive")
        return value


class TestConfig(StrictModel):
    smoke: list[SmokeTest] = Field(default_factory=list)
    scripts: list[ScriptTest] = Field(default_factory=list)
    pytest: list[PytestTest] = Field(default_factory=list)
    ssh: list[SshTest] = Field(default_factory=list)
    command_timeout_sec: int = 30
    # Post-boot status snapshot. Empty string disables. Default expects the
    # OpenWrt-shipped `ubus` returning a JSON object with release/kernel/etc.
    status_command: str = "ubus call system board"

    @field_validator("smoke", mode="before")
    @classmethod
    def normalize_smoke_entries(cls, value: Any) -> list[Any]:
        """Backwards-compat: a bare string entry still validates as just a
        command with no expect regex. Dicts pass through unchanged."""
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for entry in value:
            if isinstance(entry, str):
                normalized.append({"command": entry})
            else:
                normalized.append(entry)
        return normalized

    @field_validator("command_timeout_sec")
    @classmethod
    def test_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("tests.command_timeout_sec must be positive")
        return value


class RetryPolicy(StrictModel):
    attempts: int = 1
    backoff_sec: float = 0

    @field_validator("attempts")
    @classmethod
    def attempts_must_be_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("retry.attempts must be at least 1")
        return value

    @field_validator("backoff_sec")
    @classmethod
    def backoff_must_not_be_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("retry.backoff_sec must be 0 or greater")
        return value


class RetryConfig(StrictModel):
    artifact_select: RetryPolicy = Field(default_factory=RetryPolicy)
    artifact_export: RetryPolicy = Field(default_factory=RetryPolicy)
    firmware_transfer: RetryPolicy = Field(default_factory=RetryPolicy)
    smoke_tests: RetryPolicy = Field(default_factory=RetryPolicy)


class OwrtConfig(StrictModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    builder: BuilderConfig
    artifact: ArtifactConfig
    dut: DutConfig = Field(default_factory=DutConfig)
    upgrade: UpgradeConfig = Field(default_factory=UpgradeConfig)
    tests: TestConfig = Field(default_factory=TestConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def default_profile_must_exist(self) -> OwrtConfig:
        default_profile = self.project.default_profile
        if default_profile is not None and default_profile not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "(no profiles defined)"
            raise ValueError(
                f"project.default_profile {default_profile!r} is not defined in profiles; "
                f"available: {available}"
            )
        return self

    def artifact_root(self, config_path: Path) -> Path:
        return _resolve_path(self.project.artifact_dir, config_path.parent)

    def state_db_path(self, config_path: Path) -> Path:
        if self.project.state_db is not None:
            return _resolve_path(self.project.state_db, config_path.parent)
        return self.artifact_root(config_path) / "owrt_monitor.sqlite3"

    def redacted_dump(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        password = data.get("dut", {}).get("login", {}).get("password")
        if password:
            data["dut"]["login"]["password"] = "<redacted>"
        builder_env = data.get("builder", {}).get("env", {})
        for key in list(builder_env):
            if _is_sensitive_key(key):
                builder_env[key] = "<redacted>"
        return data

    def with_profile(self, name: str) -> OwrtConfig:
        """Return a new OwrtConfig with the named profile overlaid on the base.

        The overlay is deep-merged: nested dicts are merged recursively; lists and
        scalars from the overlay replace those in the base wholesale (so e.g.
        `builder.command` and `artifact.patterns` are replaced, not concatenated).
        """
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "(no profiles defined)"
            raise ConfigError(f"unknown profile {name!r}; available: {available}")

        overlay = self.profiles[name]
        base = self.model_dump(mode="json")
        base.pop("profiles", None)
        base["project"]["default_profile"] = None
        merged = _deep_merge(base, overlay)
        try:
            return OwrtConfig.model_validate(merged)
        except Exception as exc:
            raise ConfigError(
                f"applying profile {name!r} produced an invalid config: {exc}"
            ) from exc

    def effective_profile(self, requested: str | None) -> str | None:
        """Return the explicit profile, or the config's default profile."""
        return requested if requested is not None else self.project.default_profile

    def list_profiles(self) -> list[str]:
        return sorted(self.profiles)


def load_config(path: Path | str) -> OwrtConfig:
    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {config_path}: {exc}") from exc

    try:
        raw_data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    try:
        interpolated = _interpolate_env(raw_data)
        return OwrtConfig.model_validate(interpolated)
    except Exception as exc:
        raise ConfigError(f"invalid config {config_path}: {exc}") from exc


def _interpolate_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _interpolate_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(item) for item in value]
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_env, value)
    return value


def _replace_env(match: re.Match[str]) -> str:
    name = match.group(1)
    default = match.group(2)
    if name in os.environ:
        return os.environ[name]
    if default is not None:
        return default
    raise ConfigError(f"environment variable {name} is required by config interpolation")


def _resolve_path(path: Path, base_dir: Path) -> Path:
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(key))


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `overlay` onto `base`, returning a new dict.

    For each key:
      - if both values are dicts → recurse;
      - otherwise → overlay's value replaces base's wholesale.
    Neither input is mutated.
    """
    result: dict[str, Any] = {key: value for key, value in base.items()}
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = _deep_merge(base_value, overlay_value)
        else:
            result[key] = overlay_value
    return result
