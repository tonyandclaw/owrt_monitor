from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fake_docker import FakeDockerBuildClient
from owrt_monitor.config import BuilderConfig, ConfigError, load_config
from owrt_monitor.state import JobState
from owrt_monitor.workflow import BuildWorkflow

CLEANUP_CMD = ["make", "-C", "build/owrt2102", "package/asus-base-files/clean"]


def _write_config(tmp_path: Path, *, builder_extra: dict | None = None) -> Path:
    raw = {
        "project": {
            "name": "switch",
            "artifact_dir": str(tmp_path / "artifacts"),
        },
        "builder": {
            "container": "fake-builder",
            "workdir": "/work/openwrt",
            "command": ["make", "eap5000.profile"],
            "env": {"FORCE_UNSAFE_CONFIGURE": "1"},
            **(builder_extra or {}),
        },
        "artifact": {
            "patterns": ["build/fake/bin/target/openwrt-*-sysupgrade.bin"],
            "selection": "newest",
            "min_size_mb": 0,
        },
        "dut": {"serial": "/dev/fake"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _builder_from_config(config_path: Path) -> BuilderConfig:
    return load_config(config_path).builder


def _seed_successful_job(workflow: BuildWorkflow, *, container: str, command: list[str]) -> str:
    """Insert a prior SUCCEEDED job whose snapshot targets `command` in `container`."""
    job_id = "job_prevsuccess0"
    snapshot = {"builder": {"container": container, "command": command}}
    workflow.store.create_job(
        job_id=job_id,
        config_path=workflow.config_path,
        artifact_dir=workflow.artifact_root / job_id,
        state=JobState.PENDING.value,
        config_snapshot=snapshot,
    )
    workflow.store.update_job(
        job_id=job_id, state=JobState.SUCCEEDED.value, result="success"
    )
    return job_id


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_profile_switch_clean_runs_cleanup_before_build(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        builder_extra={
            "on_profile_switch": "clean",
            "profile_switch_cleanup": [CLEANUP_CMD],
        },
    )
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)
    prev_id = _seed_successful_job(
        workflow, container="fake-builder", command=["make", "controller.profile"]
    )

    report = workflow.run(dry_run=False, allow_flash=False)

    assert report.success is True
    # The configured cleanup ran exactly once, with the configured command.
    assert fake.run_cleanup_calls == [CLEANUP_CMD]
    # ... and it ran before the build (build still happened once).
    assert fake.run_build_calls == 1
    # Surfaced in the report + events.
    assert any("Profile switch in shared builder" in a for a in report.actions)
    assert any("Profile-switch cleanup (ran)" in a for a in report.actions)
    events = _events(report.run_dir)
    detected = [e for e in events if e["event"] == "profile_switch_detected"]
    assert detected and detected[0]["fields"]["previous_job"] == prev_id
    assert any(e["event"] == "profile_switch_cleanup_ran" for e in events)


def test_profile_switch_warn_does_not_clean(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        builder_extra={
            "on_profile_switch": "warn",
            "profile_switch_cleanup": [CLEANUP_CMD],
        },
    )
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)
    _seed_successful_job(
        workflow, container="fake-builder", command=["make", "controller.profile"]
    )

    report = workflow.run(dry_run=False, allow_flash=False)

    assert report.success is True
    assert fake.run_cleanup_calls == []
    assert any("profile switch detected" in w for w in report.warnings)


def test_no_switch_when_same_target(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        builder_extra={"on_profile_switch": "clean", "profile_switch_cleanup": [CLEANUP_CMD]},
    )
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)
    _seed_successful_job(
        workflow, container="fake-builder", command=["make", "eap5000.profile"]
    )

    report = workflow.run(dry_run=False, allow_flash=False)

    assert report.success is True
    assert fake.run_cleanup_calls == []
    assert not any(e["event"] == "profile_switch_detected" for e in _events(report.run_dir))


def test_no_switch_when_different_container(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        builder_extra={"on_profile_switch": "clean", "profile_switch_cleanup": [CLEANUP_CMD]},
    )
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)
    # Different container == a different build tree, so not a contamination risk.
    _seed_successful_job(
        workflow, container="other-builder", command=["make", "controller.profile"]
    )

    report = workflow.run(dry_run=False, allow_flash=False)

    assert report.success is True
    assert fake.run_cleanup_calls == []


def test_profile_switch_dry_run_lists_but_does_not_clean(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        builder_extra={"on_profile_switch": "clean", "profile_switch_cleanup": [CLEANUP_CMD]},
    )
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)
    _seed_successful_job(
        workflow, container="fake-builder", command=["make", "controller.profile"]
    )

    report = workflow.run(dry_run=True, allow_flash=False)

    assert report.state == JobState.DRY_RUN.value
    assert fake.run_cleanup_calls == []
    assert fake.run_build_calls == 0
    assert any("Profile-switch cleanup (planned)" in a for a in report.actions)


def test_profile_switch_cleanup_failure_is_warned_not_fatal(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        builder_extra={"on_profile_switch": "clean", "profile_switch_cleanup": [CLEANUP_CMD]},
    )
    fake = FakeDockerBuildClient(
        builder=_builder_from_config(config_path), cleanup_should_fail=True
    )
    workflow = BuildWorkflow(config_path, docker_client=fake)
    _seed_successful_job(
        workflow, container="fake-builder", command=["make", "controller.profile"]
    )

    report = workflow.run(dry_run=False, allow_flash=False)

    # A failed clean does not abort the build (the build itself surfaces breakage).
    assert report.success is True
    assert fake.run_build_calls == 1
    assert any("profile-switch cleanup failed" in w for w in report.warnings)
    assert any(
        e["event"] == "profile_switch_cleanup_failed" for e in _events(report.run_dir)
    )


def test_clean_mode_without_cleanup_commands_warns(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        builder_extra={"on_profile_switch": "clean", "profile_switch_cleanup": []},
    )
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)
    _seed_successful_job(
        workflow, container="fake-builder", command=["make", "controller.profile"]
    )

    report = workflow.run(dry_run=False, allow_flash=False)

    assert report.success is True
    assert fake.run_cleanup_calls == []
    assert any("nothing will be cleaned" in w for w in report.warnings)


def test_invalid_on_profile_switch_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, builder_extra={"on_profile_switch": "nuke"})
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_empty_cleanup_command_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, builder_extra={"profile_switch_cleanup": [[]]})
    with pytest.raises(ConfigError):
        load_config(config_path)
