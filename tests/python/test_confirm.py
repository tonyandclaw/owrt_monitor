from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml
from owrt_monitor.config import load_config
from owrt_monitor.dut_workflow import DutWorkflow, DutWorkflowError
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def _make_workflow(tmp_path: Path, *, confirm: bool) -> DutWorkflow:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "x", "workdir": "/w", "command": ["m"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {"name": "dut-c", "serial": "/dev/fake"},
        "upgrade": {
            "transfer": "http",
            "http_host": "127.0.0.1",
            "confirm_before_flash": confirm,
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(p)
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_job(
        job_id="job_c",
        config_path=p,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot={},
    )
    logger = EventLogger(store=store, job_id="job_c", path=run_dir / "events.jsonl")
    return DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_c",
    )


def test_confirm_no_op_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _make_workflow(tmp_path, confirm=False)
    # Even with stdin pretending to be a TTY, with confirm_before_flash=False
    # the helper short-circuits without calling input().
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    workflow._confirm_destructive_step("test op")  # must not raise


def test_confirm_skipped_when_stdin_not_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-TTY skip preserves automation: CI / background scripts never
    block on a prompt that nobody can answer."""
    workflow = _make_workflow(tmp_path, confirm=True)

    class _NonTty(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", _NonTty(""))
    # Should NOT raise — non-TTY → log a notice and continue.
    workflow._confirm_destructive_step("test op")


def test_confirm_proceeds_on_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _make_workflow(tmp_path, confirm=True)

    class _TtyYes(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _TtyYes(""))
    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    workflow._confirm_destructive_step("test op")


def test_confirm_aborts_on_no(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _make_workflow(tmp_path, confirm=True)

    class _TtyNo(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _TtyNo(""))
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    with pytest.raises(DutWorkflowError, match=r"declined confirmation"):
        workflow._confirm_destructive_step("test op")


def test_confirm_aborts_on_eof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _make_workflow(tmp_path, confirm=True)

    class _TtyEof(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _TtyEof(""))

    def _raise_eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    with pytest.raises(DutWorkflowError, match=r"stdin closed"):
        workflow._confirm_destructive_step("test op")
