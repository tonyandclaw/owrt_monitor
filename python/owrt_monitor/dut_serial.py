from __future__ import annotations

import glob
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SerialError(RuntimeError):
    """Raised when serial interaction fails."""


class SerialTransport(Protocol):
    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SerialCommandResult:
    command: str
    output: str
    duration_sec: float


def discover_serial_ports(patterns: list[str]) -> list[str]:
    ports: set[str] = set()
    for pattern in patterns:
        ports.update(glob.glob(pattern))
    return sorted(ports)


class SerialSession:
    def __init__(
        self,
        *,
        port: str,
        baud: int,
        prompt: str,
        transcript_path: Path,
        newline: str = "\n",
        transport: SerialTransport | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.prompt = re.compile(prompt)
        self.transcript_path = transcript_path
        self.newline = newline
        self._transport = transport

    def connect(self) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        if self._transport is not None:
            return

        try:
            import serial
        except ImportError as exc:
            raise SerialError(
                "pyserial is required for DUT serial support; install owrt-monitor[serial]"
            ) from exc

        try:
            self._transport = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=0.2,
                write_timeout=5,
            )
        except Exception as exc:
            raise SerialError(f"cannot open serial port {self.port}: {exc}") from exc

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def send_newline(self) -> None:
        self._write_text(self.newline)

    def run_command(self, command: str, *, timeout_sec: int) -> SerialCommandResult:
        started = time.monotonic()
        self._write_text(command + self.newline)
        output = self.read_until_prompt(timeout_sec=timeout_sec)
        return SerialCommandResult(
            command=command,
            output=output,
            duration_sec=time.monotonic() - started,
        )

    def write_command(self, command: str) -> None:
        self._write_text(command + self.newline)

    def read_until_prompt(self, *, timeout_sec: int) -> str:
        return self.read_until(self.prompt, timeout_sec=timeout_sec)

    def read_until(self, pattern: re.Pattern[str], *, timeout_sec: int) -> str:
        transport = self._require_transport()
        deadline = time.monotonic() + timeout_sec
        chunks: list[str] = []

        while time.monotonic() < deadline:
            waiting = max(int(getattr(transport, "in_waiting", 0)), 1)
            data = transport.read(waiting)
            if not data:
                time.sleep(0.05)
                continue

            self._append_transcript(data)
            text = data.decode("utf-8", errors="replace")
            chunks.append(text)
            output = "".join(chunks)
            if pattern.search(output):
                return output

        raise SerialError(f"timed out after {timeout_sec} seconds waiting for {pattern.pattern!r}")

    def _write_text(self, value: str) -> None:
        transport = self._require_transport()
        data = value.encode("utf-8")
        transport.write(data)
        self._append_transcript(data)

    def _append_transcript(self, data: bytes) -> None:
        with self.transcript_path.open("ab") as file:
            file.write(data)

    def _require_transport(self) -> SerialTransport:
        if self._transport is None:
            raise SerialError("serial session is not connected")
        return self._transport
