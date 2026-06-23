from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from owrt_monitor.artifacts import ExportedArtifact
from owrt_monitor.cancel import CancelToken, with_retry
from owrt_monitor.config import OwrtConfig
from owrt_monitor.dut_serial import (
    BootFailureError,
    SerialError,
    SerialSession,
    SerialTransport,
    discover_serial_ports,
)
from owrt_monitor.dut_status import DutStatus, parse_ubus_system_board
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.transfer import (
    FirmwareServerError,
    TemporaryFirmwareServer,
    TemporaryTftpFirmwareServer,
    infer_host_for_interface,
    infer_host_for_target,
)


class DutWorkflowError(RuntimeError):
    """Raised when DUT upgrade or smoke testing cannot complete."""


_SERIAL_COMMAND_ERROR_PATTERNS = [
    re.compile(r"(?im)^(?:tftp|wget|sha256sum|/bin/ash|ash|test):\s+.+$"),
    re.compile(
        r"(?i)\b(network unreachable|no such file|can't open|unknown operand|"
        r"permission denied|connection refused|bad address|not found)\b"
    ),
]
_UPGRADE_COMMAND_FAILURE_PATTERNS = [
    re.compile(r"(?im)^.*\bDevice .+ not supported by this image\b.*$"),
    re.compile(r"(?im)^.*\bImage check failed\.\s*$"),
    re.compile(r"(?im)^.*\bImage metadata not found\b.*$"),
    re.compile(r"(?im)^.*\bInvalid image\b.*$"),
]
_HOST_INTERFACE_TRANSFERS = {"http", "tftp", "bootloader_tftp"}


@dataclass(frozen=True)
class _NetworkRecoveryCleanup:
    interface: str
    static_cidr: str


def _is_upgrade_command_failure(exc: BootFailureError) -> bool:
    return any(pattern.pattern == exc.pattern for pattern in _UPGRADE_COMMAND_FAILURE_PATTERNS)


def _resolve_serial_path(serial: str | None, patterns: list[str]) -> str:
    if serial:
        return serial
    ports = discover_serial_ports(patterns)
    if not ports:
        raise DutWorkflowError(
            "no USB serial ports found; configure dut.serial or adjust dut.discovery_patterns"
        )
    if len(ports) > 1:
        raise DutWorkflowError(
            "multiple USB serial ports found; configure dut.serial explicitly: "
            + ", ".join(ports)
        )
    return ports[0]


def _configured_serial_session(
    config: OwrtConfig,
    transcript_path: Path,
    *,
    transport_factory: Callable[[], SerialTransport] | None = None,
) -> SerialSession:
    serial_path = _resolve_serial_path(config.dut.serial, config.dut.discovery_patterns)
    newline = "\r\n" if config.dut.newline == "crlf" else "\n"
    return SerialSession(
        port=serial_path,
        baud=config.dut.baud,
        prompt=config.dut.prompt,
        transcript_path=transcript_path,
        newline=newline,
        transport_factory=transport_factory,
        bytesize=config.dut.bytesize,
        parity=config.dut.parity,
        stopbits=config.dut.stopbits,
    )


def _connect_session_with_optional_login(
    config: OwrtConfig,
    session: SerialSession,
    *,
    cancel_token: CancelToken | None = None,
) -> None:
    """Open serial, send a newline, and wait for shell/login prompts."""
    session.connect()
    session.send_newline()
    timeout = config.dut.connect_timeout_sec
    login = config.dut.login
    prompt_re = re.compile(config.dut.prompt)

    if login.password is None:
        session.read_until(
            prompt_re,
            timeout_sec=timeout,
            cancel_token=cancel_token,
        )
        return

    sentinels = {
        "shell": prompt_re,
        "login": re.compile(r"[Ll]ogin:\s*$"),
        "password": re.compile(r"[Pp]assword:\s*$"),
    }
    name, _ = session.read_until_one_of(
        sentinels,
        timeout_sec=timeout,
        cancel_token=cancel_token,
    )
    if name == "shell":
        return
    if name == "login":
        session.write_command(login.username)
        name, _ = session.read_until_one_of(
            {"shell": sentinels["shell"], "password": sentinels["password"]},
            timeout_sec=timeout,
            cancel_token=cancel_token,
        )
        if name == "shell":
            return
    # `name` is now "password" — either we hit it directly, or we sent a
    # username and the device asked for a password.
    session.write_command(login.password, redact_in_transcript=True)
    session.read_until(
        prompt_re,
        timeout_sec=timeout,
        cancel_token=cancel_token,
    )


def probe_serial_interactive(
    config: OwrtConfig,
    transcript_path: Path,
    *,
    cancel_token: CancelToken | None = None,
    transport_factory: Callable[[], SerialTransport] | None = None,
) -> str:
    """Verify that the configured serial console reaches the configured prompt."""
    session = _configured_serial_session(
        config,
        transcript_path,
        transport_factory=transport_factory,
    )
    try:
        _connect_session_with_optional_login(
            config,
            session,
            cancel_token=cancel_token,
        )
        return session.port
    except (SerialError, OSError) as exc:
        raise DutWorkflowError(
            f"serial console is not interactive on {session.port}: {exc}; "
            f"expected prompt /{config.dut.prompt}/ after sending newline"
        ) from exc
    finally:
        session.close()


def _parse_busybox_df_avail_kb(output: str) -> int | None:
    """Extract the 'Available' kilobyte count from `df -k` output.

    Robust to BusyBox quirks (no header, filesystem name on its own line when long).
    Returns None on any parse failure so the caller can fall through.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    # Skip lines that look like the command echo or the header.
    candidates = []
    for line in lines:
        cols = line.split()
        if len(cols) >= 5 and cols[0] != "Filesystem" and not line.startswith("df "):
            candidates.append(cols)
    if not candidates:
        return None
    last = candidates[-1]
    # When the filesystem name wraps to its own line, BusyBox prints
    # "<fs>\n  <total> <used> <avail> <pct> <mount>" — that yields 5 cols on
    # the value line, so the avail column is at index 2 there. Discriminate
    # by inspecting whether the first token is numeric.
    try:
        if last[0].isdigit():
            return int(last[2])
        return int(last[3])
    except (ValueError, IndexError):
        return None


@dataclass(frozen=True)
class SmokeTestResult:
    command: str
    passed: bool
    output: str
    duration_sec: float
    assertion: str | None = None
    assertion_failed: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class ScriptTestResult:
    """One host-side script invocation outcome."""

    name: str
    path: str
    passed: bool
    exit_code: int
    output: str
    duration_sec: float
    timed_out: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class PytestTestResult:
    """One host-side pytest invocation outcome."""

    name: str
    path: str
    passed: bool
    exit_code: int
    output: str
    duration_sec: float
    timed_out: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class SshTestResult:
    """One SSH post-upgrade command outcome."""

    name: str
    command: str
    host: str
    passed: bool
    exit_code: int
    output: str
    duration_sec: float
    assertion: str | None = None
    assertion_failed: bool = False
    timed_out: bool = False
    skipped: bool = False


HostTestResult = ScriptTestResult | PytestTestResult | SshTestResult
StateTransition = Callable[[JobState, str, dict[str, object] | None], None]


class DutWorkflow:
    def __init__(
        self,
        *,
        config: OwrtConfig,
        run_dir: Path,
        logger: EventLogger,
        store: JobStore,
        job_id: str,
        serial_session: SerialSession | None = None,
        firmware_server: TemporaryFirmwareServer | None = None,
        cancel_token: CancelToken | None = None,
    ) -> None:
        self.config = config
        self.run_dir = run_dir
        self.logger = logger
        self.store = store
        self.job_id = job_id
        self._serial_session = serial_session
        self._firmware_server = firmware_server
        self.cancel_token = cancel_token

    def _check_cancel(self) -> None:
        if self.cancel_token is not None:
            self.cancel_token.raise_if_cancelled()

    def preflight_serial_interactive(self) -> str:
        """Fail early unless the configured serial console reaches the shell prompt."""
        if self._serial_session is not None:
            self.logger.emit(
                level="INFO",
                component="dut",
                event="serial_preflight_skipped",
                message="serial prompt preflight skipped for injected serial session",
                fields={"serial": self._serial_session.port},
            )
            return self._serial_session.port

        if not self.store.acquire_dut_lock(
            dut_name=self.config.dut.name,
            owner_job_id=self.job_id,
            lock_timeout_sec=self.config.dut.lock_timeout_sec,
        ):
            raise DutWorkflowError(f"DUT {self.config.dut.name} is already locked")

        transcript_path = self.run_dir / "serial.preflight.log"
        try:
            self.logger.emit(
                level="INFO",
                component="dut",
                event="serial_preflight_started",
                message="checking DUT serial prompt before flash/test work",
                fields={
                    "serial": self.config.dut.serial or "<auto-discover>",
                    "transcript": str(transcript_path),
                },
            )
            port = probe_serial_interactive(
                self.config,
                transcript_path,
                cancel_token=self.cancel_token,
            )
            self.logger.emit(
                level="INFO",
                component="dut",
                event="serial_preflight_succeeded",
                message="DUT serial prompt is interactive",
                fields={"serial": port, "transcript": str(transcript_path)},
            )
            return port
        finally:
            self.store.release_dut_lock(
                dut_name=self.config.dut.name,
                owner_job_id=self.job_id,
            )

    def planned_actions(self, artifact: ExportedArtifact | None = None) -> list[str]:
        serial = self.config.dut.serial or "<auto-discover>"
        filename = artifact.filename if artifact is not None else "<firmware>"
        remote_path = shlex.quote(self.config.upgrade.remote_path)
        actions = [
            f"DUT lock: `{self.config.dut.name}`",
            f"Serial console: `{serial}` at `{self.config.dut.baud}` baud",
        ]
        planned_firmware_host: str | None = None

        def firmware_host() -> str:
            nonlocal planned_firmware_host
            if planned_firmware_host is None:
                planned_firmware_host = self._firmware_host_for_plan()
            return planned_firmware_host

        if self.config.dut.expected_artifact_pattern:
            actions.append(
                f"Pre-flash gate: artifact filename must match "
                f"`/{self.config.dut.expected_artifact_pattern}/`"
            )
        recovery = self.config.upgrade.network_recovery
        if recovery.enabled:
            probe_host = recovery.ping_host or firmware_host()
            interface = recovery.interface or self.config.dut.network.interface or "<dut-interface>"
            actions.append(
                "Transfer network recovery: "
                f"ping `{probe_host}` from the serial console; if unreachable, "
                f"use `{interface}`/`{recovery.static_cidr}` as recovery hints"
            )
        if self.config.upgrade.transfer == "tftp":
            host = firmware_host()
            host_action = self._firmware_host_plan_action(host)
            if host_action is not None:
                actions.append(host_action)
            port = (
                str(self.config.upgrade.tftp_port)
                if self.config.upgrade.tftp_port
                else "<auto-port>"
            )
            actions.append(
                f"Publish firmware: copy `{filename}` to `{self.config.upgrade.tftp_root}/`"
            )
            actions.append(f"Start temporary TFTP server: `0.0.0.0:{port}`")
            actions.append(
                f"Firmware transfer: `tftp -g -r {shlex.quote(filename)} "
                f"-l {remote_path} {shlex.quote(host)} {port}`"
            )
        elif self.config.upgrade.transfer == "bootloader_tftp":
            host = firmware_host()
            host_action = self._firmware_host_plan_action(host)
            if host_action is not None:
                actions.append(host_action)
            bl = self.config.upgrade.bootloader
            actions.append(
                f"Publish firmware: copy `{filename}` to `{self.config.upgrade.tftp_root}/`"
            )
            actions.append(
                f"Reboot into bootloader: send `{bl.restart_command}` over shell, "
                f"then send {bl.interrupt_key!r} when the autoboot banner appears"
            )
            actions.append(
                f"Bootloader sequence: `setenv {bl.server_ip_env} {host}; "
                f"setenv {bl.client_ip_env} <dut-ip>; "
                f"tftpboot {bl.load_address} {filename}; {bl.boot_command}`"
            )
        elif self.config.upgrade.transfer == "custom":
            template = " ".join(
                shlex.quote(part) for part in self.config.upgrade.custom_transfer_command
            )
            actions.append(f"Firmware transfer: custom host command `{template}`")
        elif self.config.upgrade.transfer == "scp":
            host = self.config.upgrade.scp_host or self.config.dut.network.address or "<dut-ip>"
            target = f"{self.config.upgrade.scp_user}@{host}:{self.config.upgrade.remote_path}"
            actions.append(
                f"Firmware transfer: `scp {shlex.quote(filename)} {shlex.quote(target)}`"
            )
        else:
            host = firmware_host()
            host_action = self._firmware_host_plan_action(host)
            if host_action is not None:
                actions.append(host_action)
            url = f"http://{host}:<port>/{filename}"
            actions.append(
                f"Firmware transfer: `wget -O {remote_path} {shlex.quote(url)}`"
            )
        if self.config.upgrade.transfer != "bootloader_tftp":
            actions.append(f"Upgrade command: `{self.config.upgrade.command}`")
        if self.config.upgrade.expected_boot_markers:
            actions.append(
                "Post-boot gate: boot transcript must contain "
                + ", ".join(f"/{m}/" for m in self.config.upgrade.expected_boot_markers)
            )
        post_network = self.config.upgrade.post_upgrade_network
        if post_network.ensure_dhcp:
            interface = (
                post_network.interface
                or self.config.dut.network.interface
                or "<dut-interface>"
            )
            actions.append(
                f"Post-upgrade network: set `{interface}` to DHCP via UCI and reload network"
            )
        for entry in self.config.tests.smoke:
            disabled = " (disabled)" if not entry.enabled else ""
            if entry.expect:
                actions.append(
                    f"Smoke test: `{entry.command}` (expect /{entry.expect}/){disabled}"
                )
            else:
                actions.append(f"Smoke test: `{entry.command}`{disabled}")
        for entry in self.config.tests.scripts:
            command = " ".join(shlex.quote(part) for part in [entry.path, *entry.args])
            disabled = " (disabled)" if not entry.enabled else ""
            actions.append(f"Custom script: `{command}`{disabled}")
        for entry in self.config.tests.pytest:
            python = entry.python or sys.executable
            command = " ".join(
                shlex.quote(part) for part in [python, "-m", "pytest", entry.path, *entry.args]
            )
            disabled = " (disabled)" if not entry.enabled else ""
            actions.append(f"Pytest: `{command}`{disabled}")
        for entry in self.config.tests.ssh:
            host = entry.host or self.config.dut.network.address or "<dut-ip>"
            command = " ".join(
                shlex.quote(part)
                for part in [entry.ssh_binary, f"{entry.user}@{host}", entry.command]
            )
            disabled = " (disabled)" if not entry.enabled else ""
            if entry.expect:
                actions.append(f"SSH test: `{command}` (expect /{entry.expect}/){disabled}")
            else:
                actions.append(f"SSH test: `{command}`{disabled}")
        return actions

    def execute_upgrade_and_tests(
        self,
        artifact: ExportedArtifact,
        *,
        transition: StateTransition,
        metrics: dict[str, float] | None = None,
        status_out: dict[str, Any] | None = None,
        script_results_out: list[ScriptTestResult] | None = None,
        pytest_results_out: list[PytestTestResult] | None = None,
        ssh_results_out: list[SshTestResult] | None = None,
    ) -> list[SmokeTestResult]:
        transfer = self.config.upgrade.transfer
        if transfer not in {"http", "scp", "tftp", "bootloader_tftp", "custom"}:
            raise DutWorkflowError(
                f"transfer method {transfer!r} is not implemented yet"
            )

        if not self.store.acquire_dut_lock(
            dut_name=self.config.dut.name,
            owner_job_id=self.job_id,
            lock_timeout_sec=self.config.dut.lock_timeout_sec,
        ):
            raise DutWorkflowError(f"DUT {self.config.dut.name} is already locked")

        transition(JobState.DUT_LOCKED, "DUT lock acquired", {"dut": self.config.dut.name})

        session: SerialSession | None = None
        server: TemporaryFirmwareServer | TemporaryTftpFirmwareServer | None = None
        network_cleanup: _NetworkRecoveryCleanup | None = None

        try:
            session = self._serial_session or self._create_serial_session()
            self._connect_with_optional_login(session)
            self._check_cancel()
            transition(
                JobState.DUT_READY,
                "DUT serial prompt is ready",
                {"serial": self.config.dut.serial},
            )

            if transfer == "bootloader_tftp":
                host = self._firmware_host()
                self._publish_to_tftp_root(artifact)
                source_descriptor = f"bootloader-tftp://{host}/{artifact.filename}"
                self.logger.emit(
                    level="INFO",
                    component="transfer",
                    event="firmware_published",
                    message=f"published firmware to {self.config.upgrade.tftp_root}",
                    fields={
                        "tftp_root": self.config.upgrade.tftp_root,
                        "filename": artifact.filename,
                        "size_bytes": artifact.size_bytes,
                    },
                )
                self._check_cancel()
                bl = self.config.upgrade.bootloader
                self._confirm_destructive_step(
                    f"reboot {self.config.dut.name} into bootloader for TFTP recovery"
                )
                transition(
                    JobState.UPGRADE_RUNNING,
                    "rebooting into bootloader for TFTP recovery",
                    {"restart_command": bl.restart_command},
                )
                sysupgrade_started = time.monotonic()
                self._drive_bootloader_tftp(session, artifact, host)
                transition(
                    JobState.REBOOT_WAIT,
                    "bootloader sent boot command; waiting for new image to come up",
                    None,
                )
            else:
                if transfer == "http":
                    host = self._firmware_host()
                    server = self._firmware_server or TemporaryFirmwareServer(
                        directory=artifact.host_path.parent,
                        bind=self.config.upgrade.http_bind,
                        port=self.config.upgrade.http_port,
                    )
                    server.start()
                    source_url = server.url_for(artifact.filename, host=host)
                    network_cleanup = self._prepare_transfer_network_recovery(session, host)
                    try:
                        with_retry(
                            "firmware_transfer",
                            lambda: self._http_download_and_verify(
                                session, artifact, source_url
                            ),
                            policy=self.config.retry.firmware_transfer,
                            cancel_token=self.cancel_token,
                            logger=self.logger,
                        )
                    finally:
                        self._cleanup_transfer_network_recovery(session, network_cleanup)
                        network_cleanup = None
                    source_descriptor = source_url
                elif transfer == "tftp":
                    host = self._firmware_host()
                    published_path = self._publish_to_tftp_root(artifact)
                    server = TemporaryTftpFirmwareServer(
                        directory=published_path.parent,
                        port=self.config.upgrade.tftp_port,
                    )
                    server.start()
                    tftp_port = server.actual_port
                    source_descriptor = f"tftp://{host}:{tftp_port}/{artifact.filename}"
                    self.logger.emit(
                        level="INFO",
                        component="transfer",
                        event="firmware_published",
                        message=(
                            f"published firmware to {published_path} and started "
                            f"temporary TFTP server on port {tftp_port}"
                        ),
                        fields={
                            "tftp_root": self.config.upgrade.tftp_root,
                            "filename": artifact.filename,
                            "size_bytes": artifact.size_bytes,
                            "tftp_port": tftp_port,
                        },
                    )
                    network_cleanup = self._prepare_transfer_network_recovery(session, host)
                    try:
                        with_retry(
                            "firmware_transfer",
                            lambda: self._tftp_download_and_verify(
                                session,
                                artifact,
                                host,
                                tftp_port,
                            ),
                            policy=self.config.retry.firmware_transfer,
                            cancel_token=self.cancel_token,
                            logger=self.logger,
                        )
                    finally:
                        self._cleanup_transfer_network_recovery(session, network_cleanup)
                        network_cleanup = None
                elif transfer == "scp":
                    with_retry(
                        "firmware_transfer",
                        lambda: self._scp_transfer_and_verify(session, artifact),
                        policy=self.config.retry.firmware_transfer,
                        cancel_token=self.cancel_token,
                        logger=self.logger,
                    )
                    source_descriptor = self._scp_target()
                else:  # custom host-side transfer command
                    with_retry(
                        "firmware_transfer",
                        lambda: self._custom_transfer_and_verify(session, artifact),
                        policy=self.config.retry.firmware_transfer,
                        cancel_token=self.cancel_token,
                        logger=self.logger,
                    )
                    source_descriptor = "custom://" + artifact.filename
                self._check_cancel()
                transition(
                    JobState.FIRMWARE_TRANSFERRED,
                    "firmware transferred to DUT",
                    {
                        "source": source_descriptor,
                        "remote_path": self.config.upgrade.remote_path,
                    },
                )
                self._check_cancel()
                self._confirm_destructive_step(
                    f"send `{self.config.upgrade.command}` to {self.config.dut.name}"
                )
                transition(
                    JobState.UPGRADE_RUNNING,
                    "starting configured firmware upgrade command",
                    {"command": self.config.upgrade.command},
                )
                sysupgrade_started = time.monotonic()
                session.write_command(self.config.upgrade.command)
                transition(JobState.REBOOT_WAIT, "waiting for DUT to return after upgrade", None)
            failure_patterns = [
                re.compile(pattern, re.MULTILINE)
                for pattern in self.config.upgrade.boot_failure_patterns
            ]
            failure_patterns.extend(_UPGRADE_COMMAND_FAILURE_PATTERNS)
            try:
                boot_transcript = session.read_until(
                    re.compile(self.config.dut.prompt),
                    timeout_sec=self.config.upgrade.boot_timeout_sec,
                    cancel_token=self.cancel_token,
                    failure_patterns=failure_patterns,
                    reconnect_on_error=True,
                    newline_after_reconnect=True,
                )
            except BootFailureError as exc:
                if _is_upgrade_command_failure(exc):
                    self.logger.emit(
                        level="ERROR",
                        component="dut",
                        event="upgrade_command_failed",
                        message=str(exc),
                        fields={"pattern": exc.pattern, "evidence": exc.evidence},
                    )
                    raise DutWorkflowError(
                        f"DUT {self.config.dut.name} failed during firmware upgrade: "
                        f"{exc.evidence} (matched {exc.pattern!r})"
                    ) from exc
                self.logger.emit(
                    level="ERROR",
                    component="dut",
                    event="boot_failure_detected",
                    message=str(exc),
                    fields={"pattern": exc.pattern, "evidence": exc.evidence},
                )
                raise DutWorkflowError(
                    f"DUT {self.config.dut.name} failed to boot after upgrade: "
                    f"{exc.evidence} (matched {exc.pattern!r})"
                ) from exc
            self._verify_expected_boot_markers(boot_transcript)
            boot_duration_sec = time.monotonic() - sysupgrade_started
            if metrics is not None:
                metrics["boot_duration_sec"] = boot_duration_sec
                # `flash_duration_sec` is an alias surfaced for Phase 10
                # metrics aggregation; for the in-process flows that means
                # "from upgrade-command write to shell-prompt return", same
                # quantity as boot_duration_sec.
                metrics["flash_duration_sec"] = boot_duration_sec
            self.logger.emit(
                level="INFO",
                component="dut",
                event="boot_completed",
                message=f"DUT prompt returned after {boot_duration_sec:.2f}s",
                fields={"boot_duration_sec": boot_duration_sec},
            )
            transition(
                JobState.DUT_ONLINE,
                "DUT prompt returned after upgrade",
                {"boot_duration_sec": boot_duration_sec},
            )
            self._ensure_post_upgrade_network_dhcp(session)

            if status_out is not None:
                status = self._capture_dut_status(session)
                if status is not None:
                    status_out.update(status.to_dict())

            transition(JobState.TEST_RUNNING, "running DUT tests", None)
            test_started = time.monotonic()
            results = self.run_smoke_tests(session)
            script_results = (
                self.run_script_tests(artifact) if script_results_out is not None else []
            )
            pytest_results = (
                self.run_pytest_tests(artifact) if pytest_results_out is not None else []
            )
            ssh_results = self.run_ssh_tests(artifact) if ssh_results_out is not None else []
            if script_results_out is not None:
                script_results_out.extend(script_results)
            if pytest_results_out is not None:
                pytest_results_out.extend(pytest_results)
            if ssh_results_out is not None:
                ssh_results_out.extend(ssh_results)
            if metrics is not None:
                self._record_test_metrics(
                    metrics=metrics,
                    test_started=test_started,
                    smoke_results=results,
                    script_results=script_results,
                    pytest_results=pytest_results,
                    ssh_results=ssh_results,
                )
            return results
        except (SerialError, FirmwareServerError, OSError) as exc:
            raise DutWorkflowError(str(exc)) from exc
        finally:
            if session is not None and network_cleanup is not None:
                self._cleanup_transfer_network_recovery(session, network_cleanup)
            if server is not None:
                server.stop()
            if session is not None:
                session.close()
            self.store.release_dut_lock(dut_name=self.config.dut.name, owner_job_id=self.job_id)

    def execute_smoke_tests(
        self,
        *,
        transition: StateTransition,
        script_results_out: list[ScriptTestResult] | None = None,
        pytest_results_out: list[PytestTestResult] | None = None,
        ssh_results_out: list[SshTestResult] | None = None,
        metrics: dict[str, float] | None = None,
    ) -> list[SmokeTestResult]:
        if not self.store.acquire_dut_lock(
            dut_name=self.config.dut.name,
            owner_job_id=self.job_id,
            lock_timeout_sec=self.config.dut.lock_timeout_sec,
        ):
            raise DutWorkflowError(f"DUT {self.config.dut.name} is already locked")

        transition(JobState.DUT_LOCKED, "DUT lock acquired", {"dut": self.config.dut.name})
        session: SerialSession | None = None

        try:
            session = self._serial_session or self._create_serial_session()
            self._connect_with_optional_login(session)
            self._check_cancel()
            transition(
                JobState.DUT_READY,
                "DUT serial prompt is ready",
                {"serial": self.config.dut.serial},
            )
            transition(JobState.TEST_RUNNING, "running DUT tests", None)
            test_started = time.monotonic()
            results = self.run_smoke_tests(session)
            script_results = self.run_script_tests() if script_results_out is not None else []
            pytest_results = self.run_pytest_tests() if pytest_results_out is not None else []
            ssh_results = self.run_ssh_tests() if ssh_results_out is not None else []
            if script_results_out is not None:
                script_results_out.extend(script_results)
            if pytest_results_out is not None:
                pytest_results_out.extend(pytest_results)
            if ssh_results_out is not None:
                ssh_results_out.extend(ssh_results)
            if metrics is not None:
                self._record_test_metrics(
                    metrics=metrics,
                    test_started=test_started,
                    smoke_results=results,
                    script_results=script_results,
                    pytest_results=pytest_results,
                    ssh_results=ssh_results,
                )
            return results
        except (SerialError, OSError) as exc:
            raise DutWorkflowError(str(exc)) from exc
        finally:
            if session is not None:
                session.close()
            self.store.release_dut_lock(dut_name=self.config.dut.name, owner_job_id=self.job_id)

    def run_smoke_tests(self, session: SerialSession | None = None) -> list[SmokeTestResult]:
        active_session = session or self._create_serial_session()
        close_when_done = session is None
        results: list[SmokeTestResult] = []

        try:
            if session is None:
                self._connect_with_optional_login(active_session)

            for entry in self.config.tests.smoke:
                self._check_cancel()
                command = entry.command
                expect = entry.expect
                if not entry.enabled:
                    results.append(
                        SmokeTestResult(
                            command=command,
                            passed=False,
                            output="skipped by config",
                            duration_sec=0.0,
                            assertion=expect,
                            skipped=True,
                        )
                    )
                    continue
                started = time.monotonic()
                try:
                    result = with_retry(
                        f"smoke_tests:{command}",
                        lambda command=command: active_session.run_command(
                            command,
                            timeout_sec=self.config.tests.command_timeout_sec,
                            cancel_token=self.cancel_token,
                        ),
                        policy=self.config.retry.smoke_tests,
                        cancel_token=self.cancel_token,
                        logger=self.logger,
                    )
                    # Serial output includes the echoed command line and the
                    # trailing prompt, so match `expect` per-line (MULTILINE):
                    # `^`/`$` should anchor to an output line, not the echoed
                    # command. Without this, e.g. `^\d+\.\d+` against
                    # `cat /proc/uptime\r\n373659.15 ...` never matches.
                    assertion_failed = (
                        expect is not None
                        and re.search(expect, result.output, re.MULTILINE) is None
                    )
                    test_result = SmokeTestResult(
                        command=command,
                        passed=not assertion_failed,
                        output=result.output,
                        duration_sec=result.duration_sec,
                        assertion=expect,
                        assertion_failed=assertion_failed,
                    )
                except SerialError as exc:
                    test_result = SmokeTestResult(
                        command=command,
                        passed=False,
                        output=str(exc),
                        duration_sec=time.monotonic() - started,
                        assertion=expect,
                    )

                results.append(test_result)
                # Persist the DB-schema-supported subset; assertion metadata
                # rides along in report.json via asdict() at the workflow layer.
                payload = asdict(test_result)
                self.store.record_test_result(
                    job_id=self.job_id,
                    command=payload["command"],
                    passed=payload["passed"],
                    output=payload["output"],
                    duration_sec=payload["duration_sec"],
                )

            return results
        finally:
            if close_when_done:
                active_session.close()

    def _prepare_transfer_network_recovery(
        self,
        session: SerialSession,
        firmware_host: str,
    ) -> _NetworkRecoveryCleanup | None:
        recovery = self.config.upgrade.network_recovery
        if not recovery.enabled:
            return None

        ping_host = recovery.ping_host or firmware_host
        interface = recovery.interface or self.config.dut.network.interface
        if not interface:
            self.logger.emit(
                level="WARN",
                component="dut",
                event="network_recovery_skipped",
                message="network recovery enabled but no interface was configured",
                fields={"ping_host": ping_host},
            )
            return None

        if self._serial_ping(session, ping_host):
            self.logger.emit(
                level="INFO",
                component="dut",
                event="network_recovery_not_needed",
                message=f"DUT can reach {ping_host}",
                fields={"ping_host": ping_host, "interface": interface},
            )
            return None

        proto = self._network_proto_for_interface(session, interface)
        self.logger.emit(
            level="INFO",
            component="dut",
            event="network_recovery_needed",
            message=(
                f"DUT cannot reach {ping_host} from console; applying temporary "
                f"{recovery.static_cidr} to {interface}"
            ),
            fields={
                "ping_host": ping_host,
                "interface": interface,
                "proto": proto,
                "static_cidr": recovery.static_cidr,
            },
        )

        cidr = recovery.static_cidr
        quoted_interface = shlex.quote(interface)
        quoted_cidr = shlex.quote(cidr)
        session.run_command(
            f"ip link set {quoted_interface} up; "
            f"ip addr add {quoted_cidr} dev {quoted_interface} 2>/dev/null || true",
            timeout_sec=self.config.dut.command_timeout_sec,
            cancel_token=self.cancel_token,
        )
        cleanup = _NetworkRecoveryCleanup(interface=interface, static_cidr=cidr)
        self.logger.emit(
            level="INFO",
            component="dut",
            event="network_recovery_applied",
            message=f"temporarily added {cidr} to {interface}",
            fields={"ping_host": ping_host, "interface": interface, "static_cidr": cidr},
        )

        if self._serial_ping(session, ping_host):
            return cleanup

        self._cleanup_transfer_network_recovery(session, cleanup)
        raise DutWorkflowError(
            f"DUT still cannot reach {ping_host} after temporarily adding "
            f"{cidr} to {interface}"
        )

    def _cleanup_transfer_network_recovery(
        self,
        session: SerialSession,
        cleanup: _NetworkRecoveryCleanup | None,
    ) -> None:
        if cleanup is None or not self.config.upgrade.network_recovery.restore_after_transfer:
            return
        quoted_interface = shlex.quote(cleanup.interface)
        quoted_cidr = shlex.quote(cleanup.static_cidr)
        try:
            session.run_command(
                f"ip addr del {quoted_cidr} dev {quoted_interface} 2>/dev/null || true",
                timeout_sec=self.config.dut.command_timeout_sec,
                cancel_token=self.cancel_token,
            )
        except Exception as exc:
            self.logger.emit(
                level="WARN",
                component="dut",
                event="network_recovery_cleanup_failed",
                message=f"failed to remove temporary {cleanup.static_cidr}: {exc}",
                fields={
                    "interface": cleanup.interface,
                    "static_cidr": cleanup.static_cidr,
                },
            )
            return
        self.logger.emit(
            level="INFO",
            component="dut",
            event="network_recovery_restored",
            message=f"removed temporary {cleanup.static_cidr} from {cleanup.interface}",
            fields={"interface": cleanup.interface, "static_cidr": cleanup.static_cidr},
        )

    def _serial_ping(self, session: SerialSession, host: str) -> bool:
        quoted_host = shlex.quote(host)
        result = session.run_command(
            f"ping -c 1 -W 2 {quoted_host} >/dev/null 2>&1; echo OWRT_PING_RC=$?",
            timeout_sec=max(self.config.dut.command_timeout_sec, 5),
            cancel_token=self.cancel_token,
        )
        matches = list(re.finditer(r"OWRT_PING_RC=(\d+)", result.output))
        match = matches[-1] if matches else None
        return match is not None and match.group(1) == "0"

    def _network_proto_for_interface(self, session: SerialSession, interface: str) -> str | None:
        quoted_interface = shlex.quote(interface)
        command = (
            f"iface={quoted_interface}; proto=''; "
            "for s in $(uci -q show network | "
            "sed -n 's/^network\\.\\([^.=]*\\)=interface$/\\1/p'); do "
            "dev=$(uci -q get network.$s.device 2>/dev/null || true); "
            "ifname=$(uci -q get network.$s.ifname 2>/dev/null || true); "
            '[ "$s" = "$iface" ] || [ "$dev" = "$iface" ] || '
            '[ "$ifname" = "$iface" ] || continue; '
            "proto=$(uci -q get network.$s.proto 2>/dev/null || true); break; "
            "done; echo OWRT_PROTO=${proto:-unknown}"
        )
        result = session.run_command(
            command,
            timeout_sec=self.config.dut.command_timeout_sec,
            cancel_token=self.cancel_token,
        )
        matches = list(re.finditer(r"OWRT_PROTO=([^\s\r\n]+)", result.output))
        match = matches[-1] if matches else None
        if match is None:
            return None
        proto = match.group(1).strip()
        return None if proto == "unknown" else proto

    def _ensure_post_upgrade_network_dhcp(self, session: SerialSession) -> None:
        post_network = self.config.upgrade.post_upgrade_network
        if not post_network.ensure_dhcp:
            return

        interface = post_network.interface or self.config.dut.network.interface
        if not interface:
            raise DutWorkflowError(
                "upgrade.post_upgrade_network.ensure_dhcp is true, but no "
                "interface was configured"
            )

        section = self._network_section_for_interface(session, interface)
        if section is None:
            raise DutWorkflowError(
                f"cannot set {interface} to DHCP: no matching UCI network interface section"
            )

        quoted_section = shlex.quote(section)
        command = (
            f"section={quoted_section}; "
            "uci set network.$section.proto=dhcp; "
            "for opt in ipaddr netmask gateway broadcast dns ip6assign ip6hint ip6ifaceid; do "
            "uci -q delete network.$section.$opt; "
            "done; "
            "uci commit network; "
            "/etc/init.d/network reload >/dev/null 2>&1 || "
            "/etc/init.d/network restart >/dev/null 2>&1 || true; "
            "sleep 2; "
            "proto=$(uci -q get network.$section.proto 2>/dev/null || true); "
            "echo OWRT_POST_UPGRADE_NETWORK=section:$section,proto:${proto:-unknown}"
        )
        result = session.run_command(
            command,
            timeout_sec=max(self.config.dut.command_timeout_sec, 10),
            cancel_token=self.cancel_token,
        )
        matches = list(
            re.finditer(
                r"OWRT_POST_UPGRADE_NETWORK=section:([^,\s]+),proto:([^\s\r\n]+)",
                result.output,
            )
        )
        match = matches[-1] if matches else None
        if match is None or match.group(2) != "dhcp":
            raise DutWorkflowError(
                f"failed to confirm {interface} is DHCP after upgrade: {result.output.strip()}"
            )

        self.logger.emit(
            level="INFO",
            component="dut",
            event="post_upgrade_network_dhcp",
            message=f"set {interface} ({section}) to DHCP after upgrade",
            fields={"interface": interface, "section": section, "proto": match.group(2)},
        )

    def _network_section_for_interface(
        self,
        session: SerialSession,
        interface: str,
    ) -> str | None:
        quoted_interface = shlex.quote(interface)
        command = (
            f"iface={quoted_interface}; section=''; "
            "for s in $(uci -q show network | "
            "sed -n 's/^network\\.\\([^.=]*\\)=interface$/\\1/p'); do "
            "dev=$(uci -q get network.$s.device 2>/dev/null || true); "
            "ifname=$(uci -q get network.$s.ifname 2>/dev/null || true); "
            'if [ "$s" = "$iface" ] || [ "$dev" = "$iface" ] || [ "$ifname" = "$iface" ]; then '
            "section=$s; break; "
            "fi; "
            "done; "
            'if [ -z "$section" ] && [ "$iface" = "br-lan" ] && '
            "uci -q get network.lan >/dev/null 2>&1; then "
            "section=lan; "
            "fi; "
            "echo OWRT_NETWORK_SECTION=${section:-unknown}"
        )
        result = session.run_command(
            command,
            timeout_sec=self.config.dut.command_timeout_sec,
            cancel_token=self.cancel_token,
        )
        matches = list(re.finditer(r"OWRT_NETWORK_SECTION=([^\s\r\n]+)", result.output))
        match = matches[-1] if matches else None
        if match is None:
            return None
        section = match.group(1).strip()
        return None if section == "unknown" else section

    def _http_download_and_verify(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
        url: str,
    ) -> None:
        self._check_dut_free_space(session, artifact)
        remote = shlex.quote(self.config.upgrade.remote_path)
        quoted_url = shlex.quote(url)
        result = session.run_command(
            f"wget -O {remote} {quoted_url}",
            timeout_sec=self.config.upgrade.transfer_timeout_sec,
            cancel_token=self.cancel_token,
        )
        self._raise_on_serial_command_error(result.output, "HTTP firmware download")
        self._verify_remote_firmware(session, artifact)

    def _tftp_download_and_verify(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
        host: str,
        port: int,
    ) -> None:
        self._check_dut_free_space(session, artifact)
        remote = shlex.quote(self.config.upgrade.remote_path)
        filename = shlex.quote(artifact.filename)
        quoted_host = shlex.quote(host)
        quoted_port = shlex.quote(str(port))
        result = session.run_command(
            f"tftp -g -r {filename} -l {remote} {quoted_host} {quoted_port}",
            timeout_sec=self.config.upgrade.transfer_timeout_sec,
            cancel_token=self.cancel_token,
        )
        self._raise_on_serial_command_error(result.output, "TFTP firmware download")
        self._verify_remote_firmware(session, artifact)

    def _scp_transfer_and_verify(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
    ) -> None:
        self._check_dut_free_space(session, artifact)
        command = self._render_scp_command(artifact)
        self._run_host_transfer_command(
            command,
            artifact=artifact,
            description="scp transfer command",
        )
        self._verify_remote_firmware(session, artifact)

    def _custom_transfer_and_verify(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
    ) -> None:
        self._check_dut_free_space(session, artifact)
        command = self._render_custom_transfer_command(artifact)
        self._run_host_transfer_command(
            command,
            artifact=artifact,
            description="custom transfer command",
        )
        self._verify_remote_firmware(session, artifact)

    def _render_scp_command(self, artifact: ExportedArtifact) -> list[str]:
        command = [self.config.upgrade.scp_binary]
        if self.config.upgrade.scp_port != 22:
            command.extend(["-P", str(self.config.upgrade.scp_port)])
        if self.config.upgrade.scp_identity_file is not None:
            command.extend(["-i", str(self.config.upgrade.scp_identity_file)])
        command.extend(self.config.upgrade.scp_extra_args)
        command.extend([str(artifact.host_path), self._scp_target()])
        return command

    def _scp_target(self) -> str:
        host = self.config.upgrade.scp_host or self.config.dut.network.address
        if not host:
            raise DutWorkflowError(
                "upgrade.scp_host or dut.network.address is required for transfer=scp"
            )
        return f"{self.config.upgrade.scp_user}@{host}:{self.config.upgrade.remote_path}"

    def _run_host_transfer_command(
        self,
        command: list[str],
        *,
        artifact: ExportedArtifact,
        description: str,
    ) -> None:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                env=self._script_env(artifact),
                capture_output=True,
                text=True,
                timeout=self.config.upgrade.transfer_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = self._subprocess_timeout_output(exc)
            raise DutWorkflowError(
                f"{description} timed out after "
                f"{self.config.upgrade.transfer_timeout_sec}s"
                + (f": {output}" if output else "")
            ) from exc
        except OSError as exc:
            raise DutWorkflowError(f"{description} failed to start: {exc}") from exc

        duration = time.monotonic() - started
        output = (completed.stdout or "") + (completed.stderr or "")
        self.logger.emit(
            level="INFO" if completed.returncode == 0 else "ERROR",
            component="transfer",
            event="host_transfer_completed",
            message=(
                f"{description} completed"
                if completed.returncode == 0
                else f"{description} failed with exit {completed.returncode}"
            ),
            fields={
                "exit_code": completed.returncode,
                "duration_sec": duration,
                "command": command[:1],
            },
        )
        if completed.returncode != 0:
            raise DutWorkflowError(
                f"{description} exited {completed.returncode}: "
                f"{self._summarize_subprocess_output(output)}"
            )

    def _render_custom_transfer_command(self, artifact: ExportedArtifact) -> list[str]:
        context = {
            "artifact": str(artifact.host_path),
            "artifact_path": str(artifact.host_path),
            "filename": artifact.filename,
            "sha256": artifact.sha256,
            "size_bytes": str(artifact.size_bytes),
            "remote_path": self.config.upgrade.remote_path,
            "dut_name": self.config.dut.name,
            "dut_serial": self.config.dut.serial or "",
            "dut_address": self.config.dut.network.address or "",
            "run_dir": str(self.run_dir),
            "job_id": self.job_id,
        }
        rendered: list[str] = []
        try:
            for part in self.config.upgrade.custom_transfer_command:
                rendered.append(part.format_map(context))
        except KeyError as exc:
            raise DutWorkflowError(
                f"unknown custom transfer placeholder {{{exc.args[0]}}}"
            ) from exc
        return rendered

    @staticmethod
    def _subprocess_timeout_output(exc: subprocess.TimeoutExpired) -> str:
        output = ""
        if exc.stdout is not None:
            output += exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(
                "utf-8", errors="replace"
            )
        if exc.stderr is not None:
            output += exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(
                "utf-8", errors="replace"
            )
        return DutWorkflow._summarize_subprocess_output(output)

    @staticmethod
    def _summarize_subprocess_output(output: str) -> str:
        compact = output.strip()
        if not compact:
            return "<no output>"
        return compact[-1000:]

    def _raise_on_serial_command_error(self, output: str, description: str) -> None:
        for pattern in _SERIAL_COMMAND_ERROR_PATTERNS:
            match = pattern.search(output)
            if match is None:
                continue
            evidence = match.group(0).strip()
            self.logger.emit(
                level="ERROR",
                component="dut",
                event="serial_command_failed",
                message=f"{description} failed: {evidence}",
                fields={"description": description, "evidence": evidence},
            )
            raise DutWorkflowError(f"{description} failed: {evidence}")

    def _verify_remote_firmware(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
    ) -> None:
        remote = shlex.quote(self.config.upgrade.remote_path)
        size_result = session.run_command(
            f"test $(wc -c < {remote}) -eq {artifact.size_bytes}",
            timeout_sec=self.config.dut.command_timeout_sec,
            cancel_token=self.cancel_token,
        )
        self._raise_on_serial_command_error(size_result.output, "remote firmware size verification")
        if self.config.upgrade.verify_sha256:
            expected = shlex.quote(artifact.sha256)
            sha_result = session.run_command(
                f"sha256sum {remote} | grep -i ^{expected}",
                timeout_sec=self.config.dut.command_timeout_sec,
                cancel_token=self.cancel_token,
            )
            self._raise_on_serial_command_error(
                sha_result.output, "remote firmware SHA256 verification"
            )

    def _publish_to_tftp_root(self, artifact: ExportedArtifact) -> Path:
        """Copy the host-side artifact into `upgrade.tftp_root/<filename>`.

        Preserves the per-job copy in `<run_dir>/firmware/` for audit. Surface a
        clear DutWorkflowError if the destination is missing or unwritable so the
        operator knows to set up the TFTP root before flashing.
        """
        tftp_root = Path(self.config.upgrade.tftp_root)
        if not tftp_root.is_dir():
            raise DutWorkflowError(
                f"upgrade.tftp_root {tftp_root} does not exist on the host; "
                "create it (e.g. `sudo mkdir -p /private/tftpboot && "
                "sudo chmod 755 /private/tftpboot`) and ensure tftpd is configured."
            )
        destination = tftp_root / artifact.filename
        try:
            if destination.exists() and not os.access(destination, os.W_OK):
                destination.unlink()
            shutil.copy2(artifact.host_path, destination)
        except OSError as exc:
            raise DutWorkflowError(
                f"failed to publish firmware to {destination}: {exc}"
            ) from exc
        return destination

    def _capture_dut_status(self, session: SerialSession) -> DutStatus | None:
        """Run the configured status command and parse the output. Best-effort —
        failure is logged as a warning but never propagates.
        """
        command = (self.config.tests.status_command or "").strip()
        if not command:
            return None
        try:
            result = session.run_command(
                command,
                timeout_sec=self.config.tests.command_timeout_sec,
                cancel_token=self.cancel_token,
            )
        except SerialError as exc:
            self.logger.emit(
                level="WARN",
                component="dut",
                event="dut_status_capture_failed",
                message=f"status command failed: {exc}",
                fields={"command": command},
            )
            return DutStatus(parse_error=f"status command failed: {exc}")
        status = parse_ubus_system_board(result.output)
        self.logger.emit(
            level="INFO" if status.parse_error is None else "WARN",
            component="dut",
            event="dut_status_captured",
            message=(
                f"DUT status captured ({status.release_summary or 'release unknown'})"
                if status.parse_error is None
                else f"DUT status parse failed: {status.parse_error}"
            ),
            fields={
                "kernel": status.kernel,
                "hostname": status.hostname,
                "board": status.board,
                "model": status.model,
                "release_summary": status.release_summary,
                "parse_error": status.parse_error,
            },
        )
        return status

    def run_script_tests(
        self,
        artifact: ExportedArtifact | None = None,
    ) -> list[ScriptTestResult]:
        """Run each `tests.scripts[]` entry as a host-side subprocess.

        DUT context is exposed via env vars (`OWRT_DUT_NAME`, etc.) so scripts
        can reach out to the device from outside (ping, HTTP API, WiFi assoc).
        Each script's exit-0 is pass; non-zero or timeout is fail. Output is
        captured into the report and the per-job log.
        """
        results: list[ScriptTestResult] = []
        scripts = self.config.tests.scripts
        if not scripts:
            return results

        base_env = self._script_env(artifact)
        for script in scripts:
            self._check_cancel()
            if not script.enabled:
                result = ScriptTestResult(
                    name=script.name,
                    path=script.path,
                    passed=False,
                    exit_code=0,
                    output="skipped by config",
                    duration_sec=0.0,
                    skipped=True,
                )
                results.append(result)
                self._emit_host_test_event(
                    event="script_test_completed",
                    kind="script",
                    name=script.name,
                    result=result,
                )
                continue
            env = {**base_env, **script.env}
            cmd = [script.path, *script.args]
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    cmd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=script.timeout_sec,
                    check=False,
                )
                duration = time.monotonic() - started
                results.append(
                    ScriptTestResult(
                        name=script.name,
                        path=script.path,
                        passed=completed.returncode == 0,
                        exit_code=completed.returncode,
                        output=(completed.stdout or "") + (completed.stderr or ""),
                        duration_sec=duration,
                    )
                )
            except subprocess.TimeoutExpired as exc:
                duration = time.monotonic() - started
                output = ""
                if exc.stdout is not None:
                    output += exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(
                        "utf-8", errors="replace"
                    )
                if exc.stderr is not None:
                    output += exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(
                        "utf-8", errors="replace"
                    )
                results.append(
                    ScriptTestResult(
                        name=script.name,
                        path=script.path,
                        passed=False,
                        exit_code=-1,
                        output=output or f"timed out after {script.timeout_sec}s",
                        duration_sec=duration,
                        timed_out=True,
                    )
                )
            except OSError as exc:
                results.append(
                    ScriptTestResult(
                        name=script.name,
                        path=script.path,
                        passed=False,
                        exit_code=-1,
                        output=f"failed to launch script: {exc}",
                        duration_sec=time.monotonic() - started,
                    )
                )
            self._emit_host_test_event(
                event="script_test_completed",
                kind="script",
                name=script.name,
                result=results[-1],
            )
        return results

    def run_pytest_tests(
        self,
        artifact: ExportedArtifact | None = None,
    ) -> list[PytestTestResult]:
        """Run each `tests.pytest[]` entry as `python -m pytest`.

        This is the structured counterpart to custom scripts for host-side test
        suites. It uses argument arrays and exposes the same `OWRT_*` context.
        """
        results: list[PytestTestResult] = []
        entries = self.config.tests.pytest
        if not entries:
            return results

        base_env = self._script_env(artifact)
        for entry in entries:
            self._check_cancel()
            if not entry.enabled:
                result = PytestTestResult(
                    name=entry.name,
                    path=entry.path,
                    passed=False,
                    exit_code=0,
                    output="skipped by config",
                    duration_sec=0.0,
                    skipped=True,
                )
                results.append(result)
                self._emit_host_test_event(
                    event="pytest_test_completed",
                    kind="pytest",
                    name=entry.name,
                    result=result,
                )
                continue
            env = {**base_env, **entry.env}
            python = entry.python or sys.executable
            cmd = [python, "-m", "pytest", entry.path, *entry.args]
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    cmd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=entry.timeout_sec,
                    check=False,
                )
                duration = time.monotonic() - started
                results.append(
                    PytestTestResult(
                        name=entry.name,
                        path=entry.path,
                        passed=completed.returncode == 0,
                        exit_code=completed.returncode,
                        output=(completed.stdout or "") + (completed.stderr or ""),
                        duration_sec=duration,
                    )
                )
            except subprocess.TimeoutExpired as exc:
                duration = time.monotonic() - started
                results.append(
                    PytestTestResult(
                        name=entry.name,
                        path=entry.path,
                        passed=False,
                        exit_code=-1,
                        output=(
                            self._subprocess_timeout_output(exc)
                            or f"timed out after {entry.timeout_sec}s"
                        ),
                        duration_sec=duration,
                        timed_out=True,
                    )
                )
            except OSError as exc:
                results.append(
                    PytestTestResult(
                        name=entry.name,
                        path=entry.path,
                        passed=False,
                        exit_code=-1,
                        output=f"failed to launch pytest: {exc}",
                        duration_sec=time.monotonic() - started,
                    )
                )
            self._emit_host_test_event(
                event="pytest_test_completed",
                kind="pytest",
                name=entry.name,
                result=results[-1],
            )
        return results

    def run_ssh_tests(
        self,
        artifact: ExportedArtifact | None = None,
    ) -> list[SshTestResult]:
        """Run each `tests.ssh[]` entry through the host `ssh` binary."""
        results: list[SshTestResult] = []
        entries = self.config.tests.ssh
        if not entries:
            return results

        base_env = self._script_env(artifact)
        for entry in entries:
            self._check_cancel()
            host = entry.host or self.config.dut.network.address or ""
            started = time.monotonic()
            if not entry.enabled:
                result = SshTestResult(
                    name=entry.name,
                    command=entry.command,
                    host=host,
                    passed=False,
                    exit_code=0,
                    output="skipped by config",
                    duration_sec=0.0,
                    assertion=entry.expect,
                    skipped=True,
                )
                results.append(result)
                self._emit_host_test_event(
                    event="ssh_test_completed",
                    kind="ssh",
                    name=entry.name,
                    result=result,
                    fields={
                        "host": result.host,
                        "assertion_failed": result.assertion_failed,
                    },
                )
                continue
            if not host:
                result = SshTestResult(
                    name=entry.name,
                    command=entry.command,
                    host="",
                    passed=False,
                    exit_code=-1,
                    output="tests.ssh[].host or dut.network.address is required",
                    duration_sec=time.monotonic() - started,
                    assertion=entry.expect,
                )
                results.append(result)
                self._emit_host_test_event(
                    event="ssh_test_completed",
                    kind="ssh",
                    name=entry.name,
                    result=result,
                    fields={
                        "host": result.host,
                        "assertion_failed": result.assertion_failed,
                    },
                )
                continue

            cmd = self._render_ssh_command(entry, host)
            try:
                completed = subprocess.run(
                    cmd,
                    env=base_env,
                    capture_output=True,
                    text=True,
                    timeout=entry.timeout_sec,
                    check=False,
                )
                duration = time.monotonic() - started
                output = (completed.stdout or "") + (completed.stderr or "")
                assertion_failed = (
                    entry.expect is not None
                    and re.search(entry.expect, output, re.MULTILINE) is None
                )
                results.append(
                    SshTestResult(
                        name=entry.name,
                        command=entry.command,
                        host=host,
                        passed=completed.returncode == 0 and not assertion_failed,
                        exit_code=completed.returncode,
                        output=output,
                        duration_sec=duration,
                        assertion=entry.expect,
                        assertion_failed=assertion_failed,
                    )
                )
            except subprocess.TimeoutExpired as exc:
                duration = time.monotonic() - started
                results.append(
                    SshTestResult(
                        name=entry.name,
                        command=entry.command,
                        host=host,
                        passed=False,
                        exit_code=-1,
                        output=(
                            self._subprocess_timeout_output(exc)
                            or f"timed out after {entry.timeout_sec}s"
                        ),
                        duration_sec=duration,
                        assertion=entry.expect,
                        timed_out=True,
                    )
                )
            except OSError as exc:
                results.append(
                    SshTestResult(
                        name=entry.name,
                        command=entry.command,
                        host=host,
                        passed=False,
                        exit_code=-1,
                        output=f"failed to launch ssh: {exc}",
                        duration_sec=time.monotonic() - started,
                        assertion=entry.expect,
                    )
                )

            self._emit_host_test_event(
                event="ssh_test_completed",
                kind="ssh",
                name=entry.name,
                result=results[-1],
                fields={
                    "host": results[-1].host,
                    "assertion_failed": results[-1].assertion_failed,
                },
            )
        return results

    def _emit_host_test_event(
        self,
        *,
        event: str,
        kind: str,
        name: str,
        result: HostTestResult,
        fields: dict[str, object] | None = None,
    ) -> None:
        status = "skipped" if result.skipped else ("passed" if result.passed else "failed")
        message = f"{kind} `{name}` {status}"
        if not result.skipped:
            message += f" in {result.duration_sec:.2f}s"
        payload: dict[str, object] = {
            "name": name,
            "exit_code": result.exit_code,
            "passed": result.passed,
            "timed_out": result.timed_out,
            "skipped": result.skipped,
        }
        if fields:
            payload.update(fields)
        self.logger.emit(
            level="INFO" if result.passed or result.skipped else "WARN",
            component="tests",
            event=event,
            message=message,
            fields=payload,
        )

    @staticmethod
    def _record_test_metrics(
        *,
        metrics: dict[str, float],
        test_started: float,
        smoke_results: list[SmokeTestResult],
        script_results: list[ScriptTestResult],
        pytest_results: list[PytestTestResult],
        ssh_results: list[SshTestResult],
    ) -> None:
        metrics["smoke_duration_sec"] = sum(
            float(result.duration_sec) for result in smoke_results
        )
        metrics["script_duration_sec"] = DutWorkflow._sum_host_test_durations(script_results)
        metrics["pytest_duration_sec"] = DutWorkflow._sum_host_test_durations(pytest_results)
        metrics["ssh_duration_sec"] = DutWorkflow._sum_host_test_durations(ssh_results)
        metrics["test_duration_sec"] = time.monotonic() - test_started

    @staticmethod
    def _render_ssh_command(entry: Any, host: str) -> list[str]:
        cmd = [entry.ssh_binary]
        if entry.port != 22:
            cmd.extend(["-p", str(entry.port)])
        if entry.identity_file is not None:
            cmd.extend(["-i", str(entry.identity_file)])
        cmd.extend(entry.extra_args)
        cmd.extend([f"{entry.user}@{host}", entry.command])
        return cmd

    @staticmethod
    def _sum_host_test_durations(results: list[HostTestResult]) -> float:
        return sum(float(result.duration_sec) for result in results)

    def _script_env(self, artifact: ExportedArtifact | None) -> dict[str, str]:
        env = dict(os.environ)
        env["OWRT_DUT_NAME"] = self.config.dut.name
        env["OWRT_DUT_SERIAL"] = self.config.dut.serial or ""
        env["OWRT_DUT_ADDRESS"] = self.config.dut.network.address or ""
        env["OWRT_RUN_DIR"] = str(self.run_dir)
        env["OWRT_JOB_ID"] = self.job_id
        if artifact is not None:
            env["OWRT_FIRMWARE_PATH"] = str(artifact.host_path)
            env["OWRT_FIRMWARE_SHA256"] = artifact.sha256
            env["OWRT_FIRMWARE_FILENAME"] = artifact.filename
        return env

    def _confirm_destructive_step(self, description: str) -> None:
        """Interactively confirm a destructive command when configured to.

        No-op when `upgrade.confirm_before_flash` is False or stdin is not a TTY
        (CI, scripts, `run_in_background` invocations). Raises
        `DutWorkflowError` if the user answers anything other than `y`/`yes`.
        """
        import sys

        if not self.config.upgrade.confirm_before_flash:
            return
        if not sys.stdin.isatty():
            self.logger.emit(
                level="INFO",
                component="dut",
                event="confirm_skipped_non_tty",
                message=(
                    "upgrade.confirm_before_flash is set but stdin is not a TTY; "
                    "proceeding without prompting"
                ),
                fields={"description": description},
            )
            return
        prompt = (
            f"\n[CONFIRM] About to: {description}\n"
            f"  DUT: {self.config.dut.name}\n"
            f"  Serial: {self.config.dut.serial or '<auto>'}\n"
            f"Type 'y' or 'yes' to proceed, anything else to abort: "
        )
        try:
            answer = input(prompt).strip().lower()
        except EOFError as exc:
            raise DutWorkflowError(
                "confirmation requested but stdin closed before answer"
            ) from exc
        if answer not in {"y", "yes"}:
            raise DutWorkflowError(
                f"user declined confirmation for: {description}"
            )

    def _drive_bootloader_tftp(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
        host: str,
    ) -> None:
        """Reboot into U-Boot, run setenv + tftpboot + bootm.

        Volatile boot — the loaded image runs once but isn't written to flash.
        Lost on next power cycle. Useful for testing a candidate firmware
        without committing it.
        """
        bl = self.config.upgrade.bootloader
        bl_prompt = re.compile(bl.prompt)
        cmd_timeout = self.config.dut.command_timeout_sec
        failure_patterns = [
            re.compile(p, re.MULTILINE) for p in self.config.upgrade.boot_failure_patterns
        ]

        # 1. Trigger reboot from the running shell.
        session.write_command(bl.restart_command)
        # 2. Wait for the autoboot countdown banner.
        session.read_until(
            re.compile(bl.interrupt_banner),
            timeout_sec=bl.autoboot_wait_sec,
            cancel_token=self.cancel_token,
            failure_patterns=failure_patterns,
        )
        # 3. Send the interrupt key (raw — most U-Boots accept any single
        #    keystroke; no newline appended).
        session.send_raw(bl.interrupt_key)
        # 4. Wait for the bootloader prompt.
        session.read_until(
            bl_prompt,
            timeout_sec=bl.bootloader_prompt_wait_sec,
            cancel_token=self.cancel_token,
            failure_patterns=failure_patterns,
        )
        # 5. Configure the TFTP server IP.
        self._bootloader_run(session, f"setenv {bl.server_ip_env} {host}", cmd_timeout)
        # 6. Configure the DUT's IP if known. Skipped when network.address is
        #    unset — assume DHCP / pre-configured env.
        if self.config.dut.network.address:
            self._bootloader_run(
                session,
                f"setenv {bl.client_ip_env} {self.config.dut.network.address}",
                cmd_timeout,
            )
        # 7. Pull the firmware into RAM.
        filename = bl.tftp_filename or artifact.filename
        self._bootloader_run(
            session,
            f"tftpboot {bl.load_address} {filename}",
            bl.tftp_load_wait_sec,
        )
        # 8. Boot the loaded image. No read_until — the caller's post-boot
        #    wait picks up from here, watching for the OpenWrt shell prompt.
        session.write_command(bl.boot_command)

    def _bootloader_run(
        self,
        session: SerialSession,
        command: str,
        timeout_sec: int,
    ) -> str:
        """Issue `command` and wait for the bootloader prompt to come back."""
        bl_prompt = re.compile(self.config.upgrade.bootloader.prompt)
        session.write_command(command)
        return session.read_until(
            bl_prompt,
            timeout_sec=timeout_sec,
            cancel_token=self.cancel_token,
        )

    def _verify_expected_boot_markers(self, transcript: str) -> None:
        """Confirm the post-sysupgrade boot stream contains all configured
        positive-signal markers. No-op when the list is empty.

        Raises `DutWorkflowError` listing the missing patterns when one or
        more expected markers were not seen in the boot transcript.
        """
        markers = self.config.upgrade.expected_boot_markers
        if not markers:
            return
        missing: list[str] = [m for m in markers if re.search(m, transcript) is None]
        if not missing:
            self.logger.emit(
                level="INFO",
                component="dut",
                event="boot_markers_observed",
                message="all expected boot markers observed",
                fields={"markers": list(markers)},
            )
            return
        self.logger.emit(
            level="ERROR",
            component="dut",
            event="boot_markers_missing",
            message=f"{len(missing)} expected boot marker(s) missing",
            fields={"missing": missing, "expected": list(markers)},
        )
        raise DutWorkflowError(
            f"DUT {self.config.dut.name} booted but expected boot markers "
            f"were absent: {missing}. Possible wrong firmware or partial boot — "
            "review serial.log."
        )

    def _connect_with_optional_login(self, session: SerialSession) -> None:
        """Open the serial session and handle the optional login dance.

        When `dut.login.password` is None, behaves exactly like the legacy
        `connect + send_newline + read_until_prompt`. When a password is set,
        watches for `login:` and `password:` banners and replies in turn.
        Password writes are redacted in the serial transcript.
        """
        _connect_session_with_optional_login(
            self.config,
            session,
            cancel_token=self.cancel_token,
        )

    def _check_dut_free_space(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
    ) -> None:
        """Verify the DUT has enough free space for the firmware before downloading.

        Skipped when `upgrade.min_dut_free_kb == 0`. Uses BusyBox-friendly `df -k`
        whose output looks like:

            Filesystem           1K-blocks      Used Available Use% Mounted on
            tmpfs                    65536       128    65408   0% /tmp

        We parse the available column (index 3) of the last data line. If parsing
        fails for any reason, the check is skipped rather than blocking the upgrade.
        """
        threshold_kb = self.config.upgrade.min_dut_free_kb
        if threshold_kb <= 0:
            return
        remote_dir = self._remote_dir_for_firmware()
        result = session.run_command(
            f"df -k {shlex.quote(remote_dir)}",
            timeout_sec=self.config.dut.command_timeout_sec,
            cancel_token=self.cancel_token,
        )
        avail_kb = _parse_busybox_df_avail_kb(result.output)
        if avail_kb is None:
            return  # introspection failed; let the wget surface the real error
        firmware_kb = (artifact.size_bytes + 1023) // 1024
        required_kb = max(threshold_kb, firmware_kb)
        if avail_kb < required_kb:
            raise DutWorkflowError(
                f"DUT {self.config.dut.name} has {avail_kb} KB free in {remote_dir}; "
                f"need at least {required_kb} KB (firmware is {firmware_kb} KB). "
                "Free space on the DUT (e.g. delete /tmp/*.tmp) and retry."
            )

    def _remote_dir_for_firmware(self) -> str:
        remote_path = self.config.upgrade.remote_path
        idx = remote_path.rfind("/")
        return remote_path[:idx] if idx > 0 else "/tmp"

    def _create_serial_session(self) -> SerialSession:
        return _configured_serial_session(self.config, self.run_dir / "serial.log")

    def _discover_one_serial_port(self) -> str:
        return _resolve_serial_path(None, self.config.dut.discovery_patterns)

    def firmware_host_plan_action(self) -> str | None:
        if (
            self.config.upgrade.transfer not in _HOST_INTERFACE_TRANSFERS
            or self.config.upgrade.host_interface is None
        ):
            return None
        return self._firmware_host_plan_action(self._firmware_host_for_plan())

    def _firmware_host_plan_action(self, host: str) -> str | None:
        interface = self.config.upgrade.host_interface
        if interface is None or self.config.upgrade.transfer not in _HOST_INTERFACE_TRANSFERS:
            return None
        return f"Firmware host interface: `{interface}` -> `{host}`"

    def _firmware_host_for_plan(self) -> str:
        if (
            self.config.upgrade.transfer in _HOST_INTERFACE_TRANSFERS
            and self.config.upgrade.host_interface is not None
        ):
            return self._firmware_host()
        if self.config.upgrade.transfer in {"tftp", "bootloader_tftp"}:
            return self.config.upgrade.tftp_host or self.config.upgrade.http_host or "<host-ip>"
        return self.config.upgrade.http_host or "<host-ip>"

    def _firmware_host(self) -> str:
        if (
            self.config.upgrade.transfer in _HOST_INTERFACE_TRANSFERS
            and self.config.upgrade.host_interface is not None
        ):
            interface = self.config.upgrade.host_interface
            host = infer_host_for_interface(interface)
            if host is None:
                raise DutWorkflowError(
                    f"could not determine an IPv4 address for upgrade.host_interface "
                    f"{interface!r}; check the USB LAN adapter is connected and has "
                    "an address, or unset upgrade.host_interface and configure "
                    "upgrade.tftp_host/upgrade.http_host explicitly"
                )
            self.logger.emit(
                level="INFO",
                component="transfer",
                event="firmware_host_resolved",
                message=f"resolved {interface} to {host}",
                fields={"host_interface": interface, "host": host},
            )
            return host

        if self.config.upgrade.transfer in {"tftp", "bootloader_tftp"}:
            configured = self.config.upgrade.tftp_host or self.config.upgrade.http_host
            host_field = "upgrade.tftp_host"
        else:
            configured = self.config.upgrade.http_host
            host_field = "upgrade.http_host"
        if configured:
            return configured
        inferred = infer_host_for_target(self.config.dut.network.address)
        if inferred:
            return inferred
        raise DutWorkflowError(
            f"{host_field} is required when the host IP cannot be inferred from dut.network"
        )
