from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from owrt_monitor.docker_build import sha256_file
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import (
    BuildWorkflow,
    WorkflowError,
    last_progress_state,
)


def test_last_progress_state_reads_latest_state_transition(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"event":"state_transition","fields":{"state":"PREFLIGHT"}}\n'
        '{"event":"job_started","fields":{}}\n'
        '{"event":"state_transition","fields":{"state":"BUILD_RUNNING"}}\n'
        '{"event":"state_transition","fields":{"state":"ARTIFACT_EXPORTED"}}\n'
        '{"event":"job_failed","fields":{}}\n',
        encoding="utf-8",
    )
    assert last_progress_state(events) is JobState.ARTIFACT_EXPORTED


def test_last_progress_state_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert last_progress_state(tmp_path / "missing.jsonl") is None


def test_resume_rejects_when_no_such_job(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    with pytest.raises(WorkflowError, match="no job with id"):
        BuildWorkflow(config_path).resume("job_does_not_exist", dry_run=True)


def test_resume_rejects_when_state_is_not_resumable(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    workflow = BuildWorkflow(config_path)

    state_db = workflow.config.state_db_path(config_path.resolve())
    store = JobStore(state_db)
    run_dir = workflow.artifact_root / "job_pending"
    run_dir.mkdir(parents=True)
    store.create_job(
        job_id="job_pending",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=workflow.config.redacted_dump(),
    )

    with pytest.raises(WorkflowError, match="resume is only supported"):
        workflow.resume("job_pending", dry_run=True)


def test_resume_dry_run_succeeds_from_artifact_exported(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    workflow = BuildWorkflow(config_path)
    state_db = workflow.config.state_db_path(config_path.resolve())
    store = JobStore(state_db)

    run_dir = workflow.artifact_root / "job_resumable"
    (run_dir / "firmware").mkdir(parents=True)
    firmware = run_dir / "firmware" / "openwrt.bin"
    firmware.write_bytes(b"fake firmware payload")

    store.create_job(
        job_id="job_resumable",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.ARTIFACT_EXPORTED.value,
        config_snapshot=workflow.config.redacted_dump(),
    )
    store.record_artifact(
        job_id="job_resumable",
        container_path="/work/openwrt/bin/openwrt.bin",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=firmware.stat().st_size,
        sha256=sha256_file(firmware),
    )

    report = workflow.resume("job_resumable", dry_run=True)

    assert report.success is True
    assert report.state == JobState.DRY_RUN.value
    assert report.job_id == "job_resumable"
    assert report.run_dir == run_dir
    report_md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Firmware transfer" in report_md
    assert "Smoke test" in report_md


def test_resume_rejects_failed_job_with_no_recoverable_state(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    workflow = BuildWorkflow(config_path)
    state_db = workflow.config.state_db_path(config_path.resolve())
    store = JobStore(state_db)

    run_dir = workflow.artifact_root / "job_failed"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        '{"event":"state_transition","fields":{"state":"BUILD_RUNNING"}}\n',
        encoding="utf-8",
    )

    store.create_job(
        job_id="job_failed",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.FAILED.value,
        config_snapshot=workflow.config.redacted_dump(),
    )

    with pytest.raises(WorkflowError, match="resume is only supported"):
        workflow.resume("job_failed", dry_run=True)


def test_resume_dry_run_succeeds_from_build_succeeded(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    workflow = BuildWorkflow(config_path)
    state_db = workflow.config.state_db_path(config_path.resolve())
    store = JobStore(state_db)

    run_dir = workflow.artifact_root / "job_build_succeeded"
    run_dir.mkdir(parents=True)
    store.create_job(
        job_id="job_build_succeeded",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.BUILD_SUCCEEDED.value,
        config_snapshot=workflow.config.redacted_dump(),
    )

    report = workflow.resume("job_build_succeeded", dry_run=True)

    assert report.success is True
    assert report.state == JobState.DRY_RUN.value
    report_md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Artifact search patterns" in report_md


def test_resume_from_artifact_exported_without_flash_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    workflow = BuildWorkflow(config_path)
    state_db = workflow.config.state_db_path(config_path.resolve())
    store = JobStore(state_db)

    run_dir = workflow.artifact_root / "job_no_flash"
    (run_dir / "firmware").mkdir(parents=True)
    firmware = run_dir / "firmware" / "openwrt.bin"
    firmware.write_bytes(b"x")
    store.create_job(
        job_id="job_no_flash",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.ARTIFACT_EXPORTED.value,
        config_snapshot=workflow.config.redacted_dump(),
    )
    store.record_artifact(
        job_id="job_no_flash",
        container_path="/work/x",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=1,
        sha256="0" * 64,
    )

    with pytest.raises(WorkflowError, match="leaves nothing to do"):
        workflow.resume("job_no_flash", dry_run=False, allow_flash=False)


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "resume-test",
                    "artifact_dir": str(tmp_path / "artifacts"),
                },
                "builder": {
                    "container": "builder",
                    "workdir": "/work/openwrt",
                    "command": ["make"],
                },
                "artifact": {
                    "patterns": ["bin/*.bin"],
                },
                "dut": {
                    "serial": "/dev/fake",
                },
                "upgrade": {
                    "http_host": "127.0.0.1",
                },
                "tests": {
                    "smoke": ["ubus call system board"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path
