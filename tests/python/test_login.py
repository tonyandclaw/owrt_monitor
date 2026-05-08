from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from owrt_monitor.config import load_config
from owrt_monitor.dut_serial import SerialError, SerialSession
from owrt_monitor.dut_workflow import DutWorkflow
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


def test_read_until_one_of_returns_named_match(tmp_path: Path) -> None:
    transport = _FakeTransport([b"random chatter\nlogin: "])
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )
    name, output = session.read_until_one_of(
        {
            "shell": re.compile(r"root@OpenWrt:.*# "),
            "login": re.compile(r"[Ll]ogin:\s*"),
        },
        timeout_sec=1,
    )
    assert name == "login"
    assert "login:" in output


def test_read_until_one_of_times_out_when_no_match(tmp_path: Path) -> None:
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=_FakeTransport([]),
    )
    with pytest.raises(SerialError, match=r"timed out"):
        session.read_until_one_of(
            {"shell": re.compile(r"root@OpenWrt:.*# ")},
            timeout_sec=1,
        )


def test_login_flow_with_password_redacts_transcript(tmp_path: Path) -> None:
    """Full login dance: device prints `login:`, we send username, device
    prints `Password:`, we send password, then shell prompt arrives. The
    typed password must NOT appear verbatim in the serial transcript."""
    workflow, transport = _make_workflow(
        tmp_path,
        password="hunter2",
        chunks=[
            b"\nDUT login: ",
            b"\nPassword: ",
            b"\nroot@OpenWrt:/# ",
        ],
    )
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )
    workflow._connect_with_optional_login(session)

    written = b"".join(transport.writes)
    # Username and password were both written to the device.
    assert b"root\n" in written
    assert b"hunter2\n" in written
    # But the transcript must redact the password.
    transcript = (tmp_path / "serial.log").read_bytes()
    assert b"hunter2" not in transcript
    assert b"<redacted>" in transcript
    # Username is fine — not a secret.
    assert b"root\n" in transcript


def test_login_flow_skipped_when_shell_appears_directly(tmp_path: Path) -> None:
    """If the shell prompt is the first thing we see (e.g. dropbear is already
    logged in), the login dance is short-circuited."""
    workflow, transport = _make_workflow(
        tmp_path,
        password="hunter2",
        chunks=[b"\nroot@OpenWrt:/# "],
    )
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )
    workflow._connect_with_optional_login(session)

    # No username/password should have been sent.
    written = b"".join(transport.writes)
    # The very first write is the send_newline() — that's expected.
    # Aside from that single newline, no other writes.
    assert written.count(b"\n") == 1


def test_passwordless_login_uses_legacy_path(tmp_path: Path) -> None:
    """When `dut.login.password` is None, the helper falls back to
    `read_until_prompt` directly — no special branching."""
    workflow, transport = _make_workflow(
        tmp_path,
        password=None,
        chunks=[b"\nroot@OpenWrt:/# "],
    )
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )
    workflow._connect_with_optional_login(session)
    transcript = (tmp_path / "serial.log").read_bytes()
    assert b"<redacted>" not in transcript


def test_login_flow_handles_password_only_banner(tmp_path: Path) -> None:
    """Some bootloaders / minimal images go straight to `Password:` without
    a `login:` banner. Make sure we still send the password."""
    workflow, transport = _make_workflow(
        tmp_path,
        password="hunter2",
        chunks=[b"\nPassword: ", b"\nroot@OpenWrt:/# "],
    )
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )
    workflow._connect_with_optional_login(session)
    written = b"".join(transport.writes)
    assert b"hunter2\n" in written
    # Username should NOT have been sent (no `login:` banner appeared).
    assert b"root\n" not in written.replace(b"\n", b"", 1)  # ignore leading send_newline


def _make_workflow(
    tmp_path: Path,
    *,
    password: str | None,
    chunks: list[bytes],
) -> tuple[DutWorkflow, _FakeTransport]:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "fake", "workdir": "/work", "command": ["make"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {
            "name": "dut-login",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 2,
            "command_timeout_sec": 1,
            "login": {"username": "root"},
        },
        "tests": {"command_timeout_sec": 1},
    }
    if password is not None:
        raw["dut"]["login"]["password"] = password
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)

    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_job(
        job_id="job_login",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot={},
    )
    logger = EventLogger(store=store, job_id="job_login", path=run_dir / "events.jsonl")
    transport = _FakeTransport(chunks)
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_login",
    )
    return workflow, transport
