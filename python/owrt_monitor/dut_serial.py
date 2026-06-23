from __future__ import annotations

import errno
import glob
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
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


def _is_resource_busy_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "errno", None) == errno.EBUSY:
            return True
        message = str(current).lower()
        if "resource busy" in message or "errno 16" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _find_serial_port_owner_pids(port: str) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-t", port],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SerialError("lsof is required to clear a busy serial port") from exc

    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise SerialError(f"lsof failed while checking {port}: {detail}")

    current_pid = os.getpid()
    pids: list[int] = []
    for line in result.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            pid = int(value)
        except ValueError:
            continue
        if pid != current_pid and pid not in pids:
            pids.append(pid)
    return pids


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
        transport_factory: Callable[[], SerialTransport] | None = None,
        bytesize: int = 8,
        parity: str = "none",
        stopbits: int = 1,
        recover_busy_port: bool = True,
        busy_port_term_timeout_sec: float = 2.0,
        busy_port_kill_timeout_sec: float = 1.0,
        busy_port_pid_finder: Callable[[str], list[int]] | None = None,
        busy_port_killer: Callable[[int, int], None] | None = None,
        busy_port_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.port = port
        self.baud = baud
        self.prompt = re.compile(prompt)
        self.transcript_path = transcript_path
        self.newline = newline
        self._transport = transport
        self._transport_factory = transport_factory
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.recover_busy_port = recover_busy_port
        self.busy_port_term_timeout_sec = busy_port_term_timeout_sec
        self.busy_port_kill_timeout_sec = busy_port_kill_timeout_sec
        self._busy_port_pid_finder = busy_port_pid_finder or _find_serial_port_owner_pids
        self._busy_port_killer = busy_port_killer or os.kill
        self._busy_port_sleep = busy_port_sleep

    def connect(self) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        if self._transport is not None:
            return
        try:
            self._transport = self._open_transport()
            return
        except SerialError:
            raise
        except Exception as exc:
            if not self.recover_busy_port or not _is_resource_busy_error(exc):
                raise SerialError(f"cannot open serial port {self.port}: {exc}") from exc
            killed_pids = self._clear_busy_port(exc)

        try:
            self._transport = self._open_transport()
        except SerialError:
            raise
        except Exception as exc:
            pids = ", ".join(str(pid) for pid in killed_pids)
            raise SerialError(
                f"cannot open serial port {self.port} after killing busy owner PID(s) "
                f"{pids}: {exc}"
            ) from exc

    def _open_transport(self) -> SerialTransport:
        if self._transport_factory is not None:
            return self._transport_factory()
        try:
            import serial
        except ImportError as exc:
            raise SerialError(
                "pyserial is required for DUT serial support; install owrt-monitor[serial]"
            ) from exc

        return serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=self.bytesize,
            parity=_PARITY_MAP.get(self.parity, "N"),
            stopbits=self.stopbits,
            timeout=0.2,
            write_timeout=5,
        )

    def _clear_busy_port(self, open_error: BaseException) -> list[int]:
        pids = self._busy_port_pid_finder(self.port)
        if not pids:
            self._append_transcript(
                (
                    "\n[owrt-monitor] serial port busy but no owning PID was found "
                    f"for {self.port}\n"
                ).encode()
            )
            raise SerialError(
                f"cannot open serial port {self.port}: {open_error}; "
                "resource-busy recovery found no owning PID"
            ) from open_error

        self._append_transcript(
            (
                "\n[owrt-monitor] serial port busy; killing owner PID(s) "
                f"{', '.join(str(pid) for pid in pids)} before retrying\n"
            ).encode()
        )
        remaining = self._signal_busy_port_pids(
            pids,
            sig=int(signal.SIGTERM),
            timeout_sec=max(self.busy_port_term_timeout_sec, 0.0),
        )
        if remaining:
            self._append_transcript(
                (
                    "[owrt-monitor] serial port still busy; force killing owner PID(s) "
                    f"{', '.join(str(pid) for pid in remaining)}\n"
                ).encode()
            )
            self._signal_busy_port_pids(
                remaining,
                sig=int(signal.SIGKILL),
                timeout_sec=max(self.busy_port_kill_timeout_sec, 0.0),
            )
        return pids

    def _signal_busy_port_pids(self, pids: list[int], *, sig: int, timeout_sec: float) -> list[int]:
        targets = set(pids)
        for pid in sorted(targets):
            try:
                self._busy_port_killer(pid, sig)
            except ProcessLookupError:
                targets.discard(pid)
            except PermissionError as exc:
                raise SerialError(f"permission denied killing PID {pid} for {self.port}") from exc
            except OSError as exc:
                raise SerialError(f"failed to kill PID {pid} for {self.port}: {exc}") from exc
        return self._wait_until_busy_pids_release(targets, timeout_sec=timeout_sec)

    def _wait_until_busy_pids_release(self, pids: set[int], *, timeout_sec: float) -> list[int]:
        deadline = time.monotonic() + timeout_sec
        remaining = set(pids)
        while remaining:
            current = set(self._busy_port_pid_finder(self.port))
            remaining &= current
            if not remaining or time.monotonic() >= deadline:
                break
            self._busy_port_sleep(min(0.1, max(deadline - time.monotonic(), 0.0)))
        return sorted(remaining)

    def close(self) -> None:
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.close()

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
        reconnect_on_error: bool = False,
        reconnect_interval_sec: float = 1.0,
        newline_after_reconnect: bool = False,
    ) -> str:
        """Read from the serial transport until `pattern` matches the buffer.

        If any regex in `failure_patterns` matches first, a `BootFailureError` is
        raised with the offending line as evidence. This is what makes a flash
        flow surface a kernel panic in seconds instead of timing out.

        When `reconnect_on_error` is true, transient serial I/O errors are
        treated as USB serial disconnects: the current transport is closed,
        the port is reopened until the original deadline, and reading resumes.
        """
        deadline = time.monotonic() + timeout_sec
        chunks: list[str] = []
        failures = failure_patterns or []

        while time.monotonic() < deadline:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            try:
                transport = self._require_transport()
                waiting = max(int(getattr(transport, "in_waiting", 0)), 1)
                data = transport.read(waiting)
            except OSError as exc:
                if not reconnect_on_error:
                    raise
                self._reconnect_after_io_error(
                    exc,
                    deadline=deadline,
                    cancel_token=cancel_token,
                    reconnect_interval_sec=reconnect_interval_sec,
                    newline_after_reconnect=newline_after_reconnect,
                )
                continue
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

    def _reconnect_after_io_error(
        self,
        exc: OSError,
        *,
        deadline: float,
        cancel_token: CancelToken | None,
        reconnect_interval_sec: float,
        newline_after_reconnect: bool,
    ) -> None:
        self._append_transcript(
            f"\n[owrt-monitor] serial I/O error: {exc}; reconnecting\n".encode()
        )
        self._discard_transport()
        last_error: Exception = exc

        while time.monotonic() < deadline:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            try:
                self.connect()
                if newline_after_reconnect:
                    self._write_text(self.newline)
                self._append_transcript(b"[owrt-monitor] serial reconnected\n")
                return
            except (OSError, SerialError) as reconnect_exc:
                last_error = reconnect_exc
                self._discard_transport()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(max(reconnect_interval_sec, 0.0), remaining))

        raise SerialError(
            f"serial I/O error and reconnect timed out before prompt returned: {last_error}"
        ) from exc

    def _discard_transport(self) -> None:
        transport = self._transport
        self._transport = None
        if transport is None:
            return
        try:
            transport.close()
        except Exception:
            pass

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
