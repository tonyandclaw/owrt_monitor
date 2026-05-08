from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from owrt_monitor.config import ConfigError, OwrtConfig, SmokeTest, load_config
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.dut_workflow import DutWorkflow, SmokeTestResult
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


class _FakeTransport:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.writes: list[bytes] = []

    @property
    def in_waiting(self) -> int:
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, size: int = 1) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        pass


def test_string_form_smoke_entry_normalizes(tmp_path: Path) -> None:
    config = _load_config(tmp_path, smoke=["uptime"])
    assert len(config.tests.smoke) == 1
    assert config.tests.smoke[0].command == "uptime"
    assert config.tests.smoke[0].expect is None


def test_dict_form_smoke_entry_with_expect(tmp_path: Path) -> None:
    config = _load_config(
        tmp_path,
        smoke=[{"command": "ip -j addr", "expect": r'"operstate":"UP"'}],
    )
    entry = config.tests.smoke[0]
    assert entry.command == "ip -j addr"
    assert entry.expect == r'"operstate":"UP"'


def test_invalid_regex_in_expect_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"expect is not a valid regex"):
        _load_config(tmp_path, smoke=[{"command": "x", "expect": r"["}])


def test_blank_command_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"command must not be blank"):
        _load_config(tmp_path, smoke=[{"command": "   "}])


def test_smoke_passes_when_expect_matches(tmp_path: Path) -> None:
    workflow, session, transport = _make_workflow_with_serial(
        tmp_path,
        smoke=[{"command": "uptime", "expect": r"\d+:\d+"}],
        chunks=[b"root@OpenWrt:/# ", b" 12:34:56 up 0\n" + b"root@OpenWrt:/# "],
    )
    session.connect()
    session.send_newline()
    session.read_until_prompt(timeout_sec=1)
    results = workflow.run_smoke_tests(session)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, SmokeTestResult)
    assert r.passed is True
    assert r.assertion == r"\d+:\d+"
    assert r.assertion_failed is False


def test_smoke_fails_when_expect_does_not_match(tmp_path: Path) -> None:
    workflow, session, transport = _make_workflow_with_serial(
        tmp_path,
        smoke=[{"command": "ip -j addr", "expect": r'"operstate":"UP"'}],
        chunks=[b"root@OpenWrt:/# ", b'"operstate":"DOWN"\n' + b"root@OpenWrt:/# "],
    )
    session.connect()
    session.send_newline()
    session.read_until_prompt(timeout_sec=1)
    results = workflow.run_smoke_tests(session)
    r = results[0]
    assert r.passed is False
    assert r.assertion_failed is True
    assert r.assertion == r'"operstate":"UP"'
    # Output is still preserved so the user can see why it failed.
    assert "DOWN" in r.output


def test_smoke_test_dataclass_has_assertion_default() -> None:
    """SmokeTest with no expect should map to assertion=None on the result."""
    entry = SmokeTest(command="uptime")
    assert entry.expect is None


def _load_config(tmp_path: Path, *, smoke: list) -> OwrtConfig:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {
            "container": "fake",
            "workdir": "/work",
            "command": ["make"],
        },
        "artifact": {"patterns": ["*.bin"]},
        "tests": {"smoke": smoke},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _make_workflow_with_serial(
    tmp_path: Path,
    *,
    smoke: list,
    chunks: list[bytes],
) -> tuple[DutWorkflow, SerialSession, _FakeTransport]:
    config_path = tmp_path / "config.yaml"
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {
            "container": "fake",
            "workdir": "/work",
            "command": ["make"],
        },
        "artifact": {"patterns": ["*.bin"]},
        "dut": {
            "name": "dut-smoke",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
        },
        "tests": {"smoke": smoke, "command_timeout_sec": 1},
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)

    transport = _FakeTransport(chunks)
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )

    store = JobStore(tmp_path / "state.sqlite3")
    store.create_job(
        job_id="job_smoke",
        config_path=config_path,
        artifact_dir=tmp_path / "run",
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    logger = EventLogger(store=store, job_id="job_smoke", path=tmp_path / "run" / "events.jsonl")
    workflow = DutWorkflow(
        config=config,
        run_dir=tmp_path / "run",
        logger=logger,
        store=store,
        job_id="job_smoke",
        serial_session=session,
    )
    return workflow, session, transport
