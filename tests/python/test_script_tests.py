from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from owrt_monitor.config import ConfigError, load_config
from owrt_monitor.dut_workflow import DutWorkflow
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def _make_workflow(tmp_path: Path, scripts: list[dict]) -> DutWorkflow:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "x", "workdir": "/w", "command": ["m"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {
            "name": "dut-script",
            "serial": "/dev/fake",
            "network": {"address": "192.168.1.1"},
        },
        "tests": {"scripts": scripts, "command_timeout_sec": 1},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(p)
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_job(
        job_id="job_s",
        config_path=p,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot={},
    )
    logger = EventLogger(store=store, job_id="job_s", path=run_dir / "events.jsonl")
    return DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_s",
    )


def _make_script(tmp_path: Path, name: str, body: str) -> Path:
    """Create a small Python script with a shebang for direct exec."""
    path = tmp_path / name
    path.write_text(f"#!{os.sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_no_scripts_returns_empty_list(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path, scripts=[])
    assert workflow.run_script_tests() == []


def test_passing_script_recorded_as_passed(tmp_path: Path) -> None:
    script = _make_script(tmp_path, "ok.py", "import sys; sys.exit(0)")
    workflow = _make_workflow(
        tmp_path,
        scripts=[{"name": "ok", "path": str(script), "timeout_sec": 5}],
    )
    results = workflow.run_script_tests()
    assert len(results) == 1
    assert results[0].name == "ok"
    assert results[0].passed is True
    assert results[0].exit_code == 0
    assert results[0].timed_out is False


def test_failing_script_recorded_as_failed(tmp_path: Path) -> None:
    script = _make_script(
        tmp_path, "fail.py", "import sys; sys.stderr.write('boom\\n'); sys.exit(2)"
    )
    workflow = _make_workflow(
        tmp_path,
        scripts=[{"name": "fail", "path": str(script), "timeout_sec": 5}],
    )
    results = workflow.run_script_tests()
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].exit_code == 2
    assert "boom" in results[0].output


def test_script_receives_dut_env_vars(tmp_path: Path) -> None:
    script = _make_script(
        tmp_path,
        "env_check.py",
        "import os, sys; "
        "expected = {'OWRT_DUT_NAME', 'OWRT_DUT_SERIAL', 'OWRT_DUT_ADDRESS', "
        "'OWRT_RUN_DIR', 'OWRT_JOB_ID'}; "
        "missing = expected - set(os.environ); "
        "sys.exit(0 if not missing else 99)",
    )
    workflow = _make_workflow(
        tmp_path,
        scripts=[{"name": "env", "path": str(script), "timeout_sec": 5}],
    )
    results = workflow.run_script_tests()
    assert results[0].passed is True


def test_script_timeout_marks_failed_and_timed_out(tmp_path: Path) -> None:
    # Script sleeps longer than its timeout.
    script = _make_script(tmp_path, "slow.py", "import time; time.sleep(10)")
    workflow = _make_workflow(
        tmp_path,
        scripts=[{"name": "slow", "path": str(script), "timeout_sec": 1}],
    )
    results = workflow.run_script_tests()
    assert results[0].passed is False
    assert results[0].timed_out is True


def test_missing_script_path_records_failure(tmp_path: Path) -> None:
    workflow = _make_workflow(
        tmp_path,
        scripts=[{"name": "missing", "path": str(tmp_path / "no_such_file"),
                  "timeout_sec": 5}],
    )
    results = workflow.run_script_tests()
    assert results[0].passed is False
    assert "failed to launch" in results[0].output.lower()


def test_blank_name_rejected_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"name.*must not be blank"):
        _make_workflow(tmp_path, scripts=[{"name": "  ", "path": "/bin/true"}])


def test_blank_path_rejected_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"path.*must not be blank"):
        _make_workflow(tmp_path, scripts=[{"name": "x", "path": ""}])


def test_zero_timeout_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"timeout_sec must be positive"):
        _make_workflow(
            tmp_path, scripts=[{"name": "x", "path": "/bin/true", "timeout_sec": 0}]
        )
