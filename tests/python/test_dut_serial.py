from pathlib import Path

import pytest
from owrt_monitor.dut_serial import SerialError, SerialSession


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
