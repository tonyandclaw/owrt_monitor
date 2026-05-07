from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigError(ValueError):
    """Raised when a config file cannot be loaded or validated."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|private[_-]?key)", re.I
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectConfig(StrictModel):
    name: str = "owrt-monitor-lab"
    artifact_dir: Path = Path("artifacts")
    state_db: Path | None = None


class BuilderConfig(StrictModel):
    container: str
    workdir: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = 0

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


class ArtifactConfig(StrictModel):
    patterns: list[str]
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
    prompt: str = r"root@OpenWrt:.*# "
    newline: Literal["lf", "crlf"] = "lf"
    connect_timeout_sec: int = 30
    command_timeout_sec: int = 30
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


class UpgradeConfig(StrictModel):
    transfer: Literal["http", "scp", "tftp", "custom"] = "http"
    remote_path: str = "/tmp/firmware.bin"
    command: str = "sysupgrade -n /tmp/firmware.bin"
    boot_timeout_sec: int = 240
    transfer_timeout_sec: int = 180
    http_bind: str = "0.0.0.0"
    http_host: str | None = None
    http_port: int = 0
    verify_sha256: bool = True

    @field_validator("boot_timeout_sec", "transfer_timeout_sec")
    @classmethod
    def upgrade_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("upgrade timeouts must be positive")
        return value

    @field_validator("http_port")
    @classmethod
    def http_port_must_be_valid(cls, value: int) -> int:
        if value < 0 or value > 65535:
            raise ValueError("upgrade.http_port must be between 0 and 65535")
        return value


class TestConfig(StrictModel):
    smoke: list[str] = Field(default_factory=list)
    command_timeout_sec: int = 30

    @field_validator("command_timeout_sec")
    @classmethod
    def test_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("tests.command_timeout_sec must be positive")
        return value


class OwrtConfig(StrictModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    builder: BuilderConfig
    artifact: ArtifactConfig
    dut: DutConfig = Field(default_factory=DutConfig)
    upgrade: UpgradeConfig = Field(default_factory=UpgradeConfig)
    tests: TestConfig = Field(default_factory=TestConfig)

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
