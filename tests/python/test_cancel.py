from pathlib import Path

import pytest
import yaml
from owrt_monitor.cancel import CancelToken, JobCancelled, with_retry
from owrt_monitor.config import RetryPolicy
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import (
    BuildWorkflow,
    SmokeTestWorkflow,
    WorkflowError,
    cancel_marker_path,
)


def test_cancel_token_round_trip(tmp_path: Path) -> None:
    token = CancelToken(tmp_path / "cancel.flag")
    assert token.is_cancelled is False
    token.request()
    assert token.is_cancelled is True
    with pytest.raises(JobCancelled):
        token.raise_if_cancelled()
    token.clear()
    assert token.is_cancelled is False


def test_dry_run_records_cancellation_when_marker_present_before_run(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)

    artifact_root = tmp_path / "artifacts"
    pre_marker_run_dir = artifact_root / "preplaced"
    pre_marker_run_dir.mkdir(parents=True)

    workflow = BuildWorkflow(config_path)
    monkeypatched_run_dir = artifact_root / "job_predetermined"
    monkeypatched_run_dir.mkdir(parents=True)
    cancel_marker_path(monkeypatched_run_dir).write_text("requested\n", encoding="utf-8")

    import owrt_monitor.workflow as workflow_module

    original_new_id = workflow_module._new_job_id
    workflow_module._new_job_id = lambda: "job_predetermined"
    try:
        with pytest.raises(WorkflowError):
            workflow.run(dry_run=True)
    finally:
        workflow_module._new_job_id = original_new_id

    store = JobStore(artifact_root / "owrt_monitor.sqlite3")
    record = store.get_job("job_predetermined")
    assert record is not None
    assert record["state"] == JobState.CANCELLED.value
    assert record["result"] == "cancelled"


def test_smoke_test_dry_run_cancels_when_marker_present(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    artifact_root = tmp_path / "artifacts"
    job_dir = artifact_root / "job_smoke_cancel"
    job_dir.mkdir(parents=True)
    cancel_marker_path(job_dir).write_text("requested\n", encoding="utf-8")

    import owrt_monitor.workflow as workflow_module

    original = workflow_module._new_job_id
    workflow_module._new_job_id = lambda: "job_smoke_cancel"
    try:
        with pytest.raises(WorkflowError):
            SmokeTestWorkflow(config_path).run(dry_run=True)
    finally:
        workflow_module._new_job_id = original

    store = JobStore(artifact_root / "owrt_monitor.sqlite3")
    record = store.get_job("job_smoke_cancel")
    assert record is not None
    assert record["state"] == JobState.CANCELLED.value


def test_with_retry_returns_after_transient_failure() -> None:
    attempts = {"count": 0}

    def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("flaky")
        return "ok"

    result = with_retry(
        "test_step",
        flaky,
        policy=RetryPolicy(attempts=3, backoff_sec=0),
    )
    assert result == "ok"
    assert attempts["count"] == 3


def test_with_retry_re_raises_when_attempts_exhausted() -> None:
    def always_fails() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        with_retry(
            "test_step",
            always_fails,
            policy=RetryPolicy(attempts=2, backoff_sec=0),
        )


def test_with_retry_propagates_job_cancelled_without_retry() -> None:
    attempts = {"count": 0}

    def cancelled() -> None:
        attempts["count"] += 1
        raise JobCancelled("cancelled mid-step")

    with pytest.raises(JobCancelled):
        with_retry(
            "test_step",
            cancelled,
            policy=RetryPolicy(attempts=5, backoff_sec=0),
        )
    assert attempts["count"] == 1


def test_get_job_returns_pid_and_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    store = JobStore(db_path)
    store.create_job(
        job_id="job_with_pid",
        config_path=tmp_path / "config.yaml",
        artifact_dir=tmp_path / "run",
        state=JobState.PENDING.value,
        config_snapshot={"project": {"name": "x"}},
        pid=4242,
    )
    record = store.get_job("job_with_pid")
    assert record is not None
    assert record["pid"] == 4242
    assert record["config_snapshot"] == {"project": {"name": "x"}}


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
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
                    "connect_timeout_sec": 1,
                    "command_timeout_sec": 1,
                },
                "upgrade": {
                    "http_host": "127.0.0.1",
                    "boot_timeout_sec": 1,
                    "transfer_timeout_sec": 1,
                },
                "tests": {
                    "smoke": ["ubus call system board"],
                    "command_timeout_sec": 1,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path
