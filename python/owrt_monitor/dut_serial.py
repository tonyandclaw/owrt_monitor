from __future__ import annotations

import glob
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from owrt_monitor.cancel import CancelToken


class SerialError(RuntimeError):
    """Raised when serial interaction fails."""


class BootFailureError(SerialError):
    """Raised when a configured boot-failure pattern matches during a serial read.

    `evidence` holds the line that triggered the match so callers can surface
    it cleanly in reports without re-parsing the transcript.
    """

    def __init__(self, pattern: str, evidence: str) -> None:
        super().__init__(f"boot failure detected (pattern {pattern!r}): {evidence}")
        self.pattern = pattern
        self.evidence = evidence


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


def _line_around(text: str, index: int) -> str:
    """Extract the single line that contains the character at `index`.

    Used to give boot-failure evidence the immediate context (one line) without
    pulling in the entire boot transcript.
    """
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def discover_serial_ports(patterns: list[str]) -> list[str]:
    ports: set[str] = set()
    for pattern in patterns:
        ports.update(glob.glob(pattern))
    return sorted(ports)


_PARITY_MAP = {
    "none": "N",
    "even": "E",
    "odd": "O",
    "mark": "M",
    "space": "S",
}


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
        bytesize: int = 8,
        parity: str = "none",
        stopbits: int = 1,
    ) -> None:
        self.port = port
        self.baud = baud
        self.prompt = re.compile(prompt)
        self.transcript_path = transcript_path
        self.newline = newline
        self._transport = transport
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits

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
                bytesize=self.bytesize,
                parity=_PARITY_MAP.get(self.parity, "N"),
                stopbits=self.stopbits,
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

    def run_command(
        self,
        command: str,
        *,
        timeout_sec: int,
        cancel_token: CancelToken | None = None,
    ) -> SerialCommandResult:
        started = time.monotonic()
        self._write_text(command + self.newline)
        output = self.read_until_prompt(timeout_sec=timeout_sec, cancel_token=cancel_token)
        return SerialCommandResult(
            command=command,
            output=output,
            duration_sec=time.monotonic() - started,
        )

    def write_command(self, command: str, *, redact_in_transcript: bool = False) -> None:
        self._write_text(command + self.newline, redact_in_transcript=redact_in_transcript)

    def send_raw(self, value: str, *, redact_in_transcript: bool = False) -> None:
        """Write raw bytes to the device WITHOUT appending a newline.

        Useful for interrupting bootloader autoboot countdowns where any
        keystroke (often just a space) drops the device into the shell.
        """
        self._write_text(value, redact_in_transcript=redact_in_transcript)

    def read_until_prompt(
        self,
        *,
        timeout_sec: int,
        cancel_token: CancelToken | None = None,
    ) -> str:
        return self.read_until(self.prompt, timeout_sec=timeout_sec, cancel_token=cancel_token)

    def read_until_one_of(
        self,
        patterns: dict[str, re.Pattern[str]],
        *,
        timeout_sec: int,
        cancel_token: CancelToken | None = None,
        failure_patterns: list[re.Pattern[str]] | None = None,
    ) -> tuple[str, str]:
        """Read until any of the named patterns matches the buffer.

        Returns `(name, full_output)` where `name` is the dict key whose regex
        fired first. Useful for branching prompts like "shell vs login banner".
        Honors cancellation and `failure_patterns` the same way `read_until` does.
        """
        if not patterns:
            raise SerialError("read_until_one_of requires at least one pattern")
        transport = self._require_transport()
        deadline = time.monotonic() + timeout_sec
        chunks: list[str] = []
        failures = failure_patterns or []
        while time.monotonic() < deadline:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            waiting = max(int(getattr(transport, "in_waiting", 0)), 1)
            data = transport.read(waiting)
            if not data:
                time.sleep(0.05)
                continue
            self._append_transcript(data)
            text = data.decode("utf-8", errors="replace")
            chunks.append(text)
            output = "".join(chunks)
            for failure in failures:
                m = failure.search(output)
                if m is not None:
                    line = _line_around(output, m.start())
                    raise BootFailureError(pattern=failure.pattern, evidence=line)
            for name, pattern in patterns.items():
                if pattern.search(output) is not None:
                    return (name, output)
        names = ", ".join(sorted(patterns))
        raise SerialError(
            f"timed out after {timeout_sec} seconds waiting for one of {names}"
        )

    def read_until(
        self,
        pattern: re.Pattern[str],
        *,
        timeout_sec: int,
        cancel_token: CancelToken | None = None,
        failure_patterns: list[re.Pattern[str]] | None = None,
    ) -> str:
        """Read from the serial transport until `pattern` matches the buffer.

        If any regex in `failure_patterns` matches first, a `BootFailureError` is
        raised with the offending line as evidence. This is what makes a flash
        flow surface a kernel panic in seconds instead of timing out.
        """
        transport = self._require_transport()
        deadline = time.monotonic() + timeout_sec
        chunks: list[str] = []
        failures = failure_patterns or []

        while time.monotonic() < deadline:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            waiting = max(int(getattr(transport, "in_waiting", 0)), 1)
            data = transport.read(waiting)
            if not data:
                time.sleep(0.05)
                continue

            self._append_transcript(data)
            text = data.decode("utf-8", errors="replace")
            chunks.append(text)
            output = "".join(chunks)
            for failure in failures:
                match = failure.search(output)
                if match is not None:
                    line = _line_around(output, match.start())
                    raise BootFailureError(pattern=failure.pattern, evidence=line)
            if pattern.search(output):
                return output

        raise SerialError(f"timed out after {timeout_sec} seconds waiting for {pattern.pattern!r}")

    def _write_text(self, value: str, *, redact_in_transcript: bool = False) -> None:
        transport = self._require_transport()
        data = value.encode("utf-8")
        transport.write(data)
        if redact_in_transcript:
            self._append_transcript(b"<redacted>" + self.newline.encode("utf-8"))
        else:
            self._append_transcript(data)

    def _append_transcript(self, data: bytes) -> None:
        with self.transcript_path.open("ab") as file:
            file.write(data)

    def _require_transport(self) -> SerialTransport:
        if self._transport is None:
            raise SerialError("serial session is not connected")
        return self._transport
