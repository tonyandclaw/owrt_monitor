import re
from pathlib import Path

import pytest
import yaml
from owrt_monitor.config import load_config
from owrt_monitor.dut_serial import BootFailureError, SerialError, SerialSession
from owrt_monitor.dut_workflow import DutWorkflowError, probe_serial_interactive


class FakeTransport:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.writes: list[bytes] = []
        self.closed = False

    @property
    def in_waiting(self) -> int:
        if not self.chunks:
            return 0
        return len(self.chunks[0])

    def read(self, size: int = 1) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class FailingReadTransport(FakeTransport):
    def read(self, size: int = 1) -> bytes:
        raise OSError("device disconnected")


def test_run_command_waits_for_prompt_and_writes_transcript(tmp_path: Path) -> None:
    transport = FakeTransport([b"OpenWrt\n", b"root@OpenWrt:/# "])
    transcript = tmp_path / "serial.log"
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=transcript,
        transport=transport,
    )

    result = session.run_command("ubus call system board", timeout_sec=1)

    assert result.command == "ubus call system board"
    assert "OpenWrt" in result.output
    assert transport.writes == [b"ubus call system board\n"]
    assert b"ubus call system board" in transcript.read_bytes()
    assert b"root@OpenWrt" in transcript.read_bytes()


def test_read_until_prompt_times_out(tmp_path: Path) -> None:
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=FakeTransport([]),
    )

    with pytest.raises(SerialError):
        session.read_until_prompt(timeout_sec=1)


def test_read_until_short_circuits_on_failure_pattern(tmp_path: Path) -> None:
    """A kernel-panic-shaped line in the stream should fail fast with the matched
    line as evidence, instead of timing out waiting for a prompt that never comes.
    """
    transport = FakeTransport(
        [
            b"[    1.234567] Booting Linux on physical CPU 0x0\n",
            b"[    2.345678] Kernel panic - not syncing: Attempted to kill init!\n",
            b"[    2.345700] CPU: 0 PID: 1 Comm: init Not tainted\n",
        ]
    )
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )

    with pytest.raises(BootFailureError) as exc_info:
        session.read_until(
            re.compile(r"root@OpenWrt:.*# "),
            timeout_sec=5,
            failure_patterns=[re.compile(r"Kernel panic - not syncing")],
        )
    err = exc_info.value
    assert "Kernel panic" in err.evidence
    assert "Attempted to kill init" in err.evidence
    assert err.pattern == "Kernel panic - not syncing"


def test_read_until_can_reconnect_after_serial_io_error(tmp_path: Path) -> None:
    first = FailingReadTransport([])
    second = FakeTransport([b"OpenWrt booted\n", b"root@OpenWrt:/# "])
    created = [first, second]

    def factory() -> FakeTransport:
        return created.pop(0)

    transcript = tmp_path / "serial.log"
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"root@OpenWrt:.*# ",
        transcript_path=transcript,
        transport_factory=factory,
    )
    session.connect()

    output = session.read_until(
        re.compile(r"root@OpenWrt:.*# "),
        timeout_sec=1,
        reconnect_on_error=True,
        reconnect_interval_sec=0,
        newline_after_reconnect=True,
    )

    assert "OpenWrt booted" in output
    assert first.closed is True
    assert second.writes == [b"\n"]
    transcript_text = transcript.read_text(encoding="utf-8")
    assert "serial I/O error" in transcript_text
    assert "serial reconnected" in transcript_text


def test_probe_serial_interactive_sends_newline_and_matches_prompt(tmp_path: Path) -> None:
    config = _probe_config(tmp_path)
    transport = FakeTransport([b"\nroot@OpenWrt:/# "])

    port = probe_serial_interactive(
        config,
        tmp_path / "serial.preflight.log",
        transport_factory=lambda: transport,
    )

    assert port == "/dev/fake"
    assert transport.writes == [b"\n"]
    assert transport.closed is True
    assert "root@OpenWrt" in (tmp_path / "serial.preflight.log").read_text(encoding="utf-8")


def test_probe_serial_interactive_reports_noninteractive_prompt(tmp_path: Path) -> None:
    config = _probe_config(tmp_path)

    with pytest.raises(DutWorkflowError, match=r"serial console is not interactive"):
        probe_serial_interactive(
            config,
            tmp_path / "serial.preflight.log",
            transport_factory=lambda: FakeTransport([]),
        )


def _probe_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {"artifact_dir": str(tmp_path / "artifacts")},
                "builder": {"container": "fake", "workdir": "/work", "command": ["make"]},
                "artifact": {"patterns": ["*.bin"]},
                "dut": {
                    "serial": "/dev/fake",
                    "prompt": r"root@OpenWrt:.*# ",
                    "connect_timeout_sec": 1,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return load_config(config_path)
