from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from owrt_monitor.config import ConfigError, load_config
from owrt_monitor.workflow import BuildWorkflow


def _write_config(
    tmp_path: Path,
    profiles: dict | None = None,
    default_profile: str | None = None,
) -> Path:
    raw: dict = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {
            "container": "builder",
            "workdir": "/work",
            "command": ["make"],
            "env": {"FORCE_UNSAFE_CONFIGURE": "1"},
        },
        "artifact": {
            "patterns": ["build/default/openwrt-*-sysupgrade.bin"],
            "selection": "newest",
            "min_size_mb": 1,
        },
        "dut": {"serial": "/dev/fake"},
    }
    if default_profile is not None:
        raw["project"]["default_profile"] = default_profile
    if profiles is not None:
        raw["profiles"] = profiles
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_with_profile_overlays_command_and_pattern(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={
            "ap-be5000": {
                "builder": {"command": ["make", "owrt2102.asus_eap5000_mt7987"]},
                "artifact": {
                    "patterns": [
                        "build/owrt2102/bin/target/openwrt-*-ASUS-EAP5000-sysupgrade.bin"
                    ]
                },
            },
        },
    )
    base = load_config(config_path)
    assert base.builder.command == ["make"]

    ap_be5000 = base.with_profile("ap-be5000")
    assert ap_be5000.builder.command == ["make", "owrt2102.asus_eap5000_mt7987"]
    assert ap_be5000.artifact.patterns == [
        "build/owrt2102/bin/target/openwrt-*-ASUS-EAP5000-sysupgrade.bin"
    ]
    # Base config must be unchanged.
    assert base.builder.command == ["make"]


def test_with_profile_deep_merges_nested_dicts(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={
            "verbose": {
                "builder": {"env": {"V": "s"}},  # adds V, keeps FORCE_UNSAFE_CONFIGURE
            },
        },
    )
    cfg = load_config(config_path).with_profile("verbose")
    assert cfg.builder.env == {"FORCE_UNSAFE_CONFIGURE": "1", "V": "s"}


def test_with_profile_unknown_name_raises(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={"ap-be5000": {"builder": {"command": ["m"]}}},
    )
    cfg = load_config(config_path)
    with pytest.raises(ConfigError, match="unknown profile 'switch'"):
        cfg.with_profile("switch")


def test_with_profile_invalid_overlay_surfaces_validation_error(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={
            "broken": {"builder": {"command": []}},  # violates "command must not be empty"
        },
    )
    cfg = load_config(config_path)
    with pytest.raises(ConfigError, match="invalid config"):
        cfg.with_profile("broken")


def test_workflow_accepts_profile_param(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={
            "ap-be5000": {
                "builder": {"command": ["make", "owrt2102.asus_eap5000_mt7987"]},
            },
        },
    )
    workflow = BuildWorkflow(config_path, profile="ap-be5000")
    assert workflow.profile == "ap-be5000"
    assert workflow.config.builder.command[-1] == "owrt2102.asus_eap5000_mt7987"


def test_workflow_uses_project_default_profile(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={
            "ap-be5000": {
                "builder": {"command": ["make", "owrt2102.asus_eap5000_mt7987"]},
            },
        },
        default_profile="ap-be5000",
    )

    workflow = BuildWorkflow(config_path)

    assert workflow.profile == "ap-be5000"
    assert workflow.config.builder.command[-1] == "owrt2102.asus_eap5000_mt7987"


def test_unknown_default_profile_raises(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={"ap-be5000": {"builder": {"command": ["m"]}}},
        default_profile="missing",
    )
    with pytest.raises(ConfigError, match="project.default_profile 'missing'"):
        load_config(config_path)


def test_default_profile_requires_profiles_block(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, default_profile="ap-be5000")
    with pytest.raises(ConfigError, match="no profiles defined"):
        load_config(config_path)


def test_workflow_unknown_profile_raises(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        profiles={"ap-be5000": {"builder": {"command": ["m"]}}},
    )
    with pytest.raises(ConfigError, match="unknown profile"):
        BuildWorkflow(config_path, profile="not-a-profile")


def test_no_profiles_block_is_backwards_compatible(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)  # no profiles
    cfg = load_config(config_path)
    assert cfg.profiles == {}
    assert cfg.list_profiles() == []
    # Workflow without profile param still works.
    BuildWorkflow(config_path)
