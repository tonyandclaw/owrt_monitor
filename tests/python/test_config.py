from pathlib import Path

import pytest
from owrt_monitor.config import ConfigError, load_config


def test_load_example_config() -> None:
    config = load_config(Path("configs/example.yaml"))

    assert config.project.name == "owrt-monitor-lab"
    assert config.builder.container == "openwrtbuild"
    assert config.artifact.selection == "newest"


def test_env_interpolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWRT_BUILDER", "test-builder")
    path = tmp_path / "config.yaml"
    path.write_text(
        """
builder:
  container: ${OWRT_BUILDER}
  workdir: /work/openwrt
  command: [make]
artifact:
  patterns: ["bin/*.bin"]
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.builder.container == "test-builder"


def test_missing_env_interpolation_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
builder:
  container: ${MISSING_OWRT_BUILDER}
  workdir: /work/openwrt
  command: [make]
artifact:
  patterns: ["bin/*.bin"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(path)


def test_redacted_dump_masks_sensitive_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
builder:
  container: test-builder
  workdir: /work/openwrt
  command: [make]
  env:
    NORMAL_FLAG: "1"
    API_TOKEN: super-secret
artifact:
  patterns: ["bin/*.bin"]
dut:
  login:
    username: root
    password: root-secret
""",
        encoding="utf-8",
    )

    config = load_config(path)
    redacted = config.redacted_dump()

    assert redacted["builder"]["env"]["NORMAL_FLAG"] == "1"
    assert redacted["builder"]["env"]["API_TOKEN"] == "<redacted>"
    assert redacted["dut"]["login"]["password"] == "<redacted>"
