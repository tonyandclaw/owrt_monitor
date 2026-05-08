from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
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
    discover_serial_ports,
)
from owrt_monitor.dut_status import DutStatus, parse_ubus_system_board
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.transfer import (
    FirmwareServerError,
    TemporaryFirmwareServer,
    infer_host_for_target,
)


class DutWorkflowError(RuntimeError):
    """Raised when DUT upgrade or smoke testing cannot complete."""


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

    def planned_actions(self, artifact: ExportedArtifact | None = None) -> list[str]:
        serial = self.config.dut.serial or "<auto-discover>"
        filename = artifact.filename if artifact is not None else "<firmware>"
        remote_path = shlex.quote(self.config.upgrade.remote_path)
        actions = [
            f"DUT lock: `{self.config.dut.name}`",
            f"Serial console: `{serial}` at `{self.config.dut.baud}` baud",
        ]
        if self.config.dut.expected_artifact_pattern:
            actions.append(
                f"Pre-flash gate: artifact filename must match "
                f"`/{self.config.dut.expected_artifact_pattern}/`"
            )
        if self.config.upgrade.transfer == "tftp":
            host = self.config.upgrade.tftp_host or self.config.upgrade.http_host or "<host-ip>"
            actions.append(
                f"Publish firmware: copy `{filename}` to `{self.config.upgrade.tftp_root}/`"
            )
            actions.append(
                f"Firmware transfer: `tftp -g -r {shlex.quote(filename)} "
                f"-l {remote_path} {shlex.quote(host)}`"
            )
        elif self.config.upgrade.transfer == "bootloader_tftp":
            host = self.config.upgrade.tftp_host or self.config.upgrade.http_host or "<host-ip>"
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
        else:
            host = self.config.upgrade.http_host or "<host-ip>"
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
        for entry in self.config.tests.smoke:
            if entry.expect:
                actions.append(f"Smoke test: `{entry.command}` (expect /{entry.expect}/)")
            else:
                actions.append(f"Smoke test: `{entry.command}`")
        return actions

    def execute_upgrade_and_tests(
        self,
        artifact: ExportedArtifact,
        *,
        transition: StateTransition,
        metrics: dict[str, float] | None = None,
        status_out: dict[str, Any] | None = None,
        script_results_out: list[ScriptTestResult] | None = None,
    ) -> list[SmokeTestResult]:
        transfer = self.config.upgrade.transfer
        if transfer not in {"http", "tftp", "bootloader_tftp"}:
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
        server: TemporaryFirmwareServer | None = None

        try:
            session = self._serial_session or self._create_serial_session()
            self._connect_with_optional_login(session)
            self._check_cancel()
            transition(
                JobState.DUT_READY,
                "DUT serial prompt is ready",
                {"serial": self.config.dut.serial},
            )

            host = self._firmware_host()
            if transfer == "bootloader_tftp":
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
                    server = self._firmware_server or TemporaryFirmwareServer(
                        directory=artifact.host_path.parent,
                        bind=self.config.upgrade.http_bind,
                        port=self.config.upgrade.http_port,
                    )
                    server.start()
                    source_url = server.url_for(artifact.filename, host=host)
                    with_retry(
                        "firmware_transfer",
                        lambda: self._http_download_and_verify(session, artifact, source_url),
                        policy=self.config.retry.firmware_transfer,
                        cancel_token=self.cancel_token,
                        logger=self.logger,
                    )
                    source_descriptor = source_url
                else:  # tftp (OpenWrt-shell)
                    published_path = self._publish_to_tftp_root(artifact)
                    source_descriptor = f"tftp://{host}/{artifact.filename}"
                    self.logger.emit(
                        level="INFO",
                        component="transfer",
                        event="firmware_published",
                        message=f"published firmware to {published_path}",
                        fields={
                            "tftp_root": self.config.upgrade.tftp_root,
                            "filename": artifact.filename,
                            "size_bytes": artifact.size_bytes,
                        },
                    )
                    with_retry(
                        "firmware_transfer",
                        lambda: self._tftp_download_and_verify(session, artifact, host),
                        policy=self.config.retry.firmware_transfer,
                        cancel_token=self.cancel_token,
                        logger=self.logger,
                    )
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
            try:
                boot_transcript = session.read_until(
                    re.compile(self.config.dut.prompt),
                    timeout_sec=self.config.upgrade.boot_timeout_sec,
                    cancel_token=self.cancel_token,
                    failure_patterns=failure_patterns,
                )
            except BootFailureError as exc:
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

            if status_out is not None:
                status = self._capture_dut_status(session)
                if status is not None:
                    status_out.update(status.to_dict())

            transition(JobState.TEST_RUNNING, "running smoke tests", None)
            smoke_started = time.monotonic()
            results = self.run_smoke_tests(session)
            if script_results_out is not None:
                script_results_out.extend(self.run_script_tests(artifact))
            if metrics is not None:
                metrics["smoke_duration_sec"] = time.monotonic() - smoke_started
            return results
        except (SerialError, FirmwareServerError, OSError) as exc:
            raise DutWorkflowError(str(exc)) from exc
        finally:
            if server is not None:
                server.stop()
            if session is not None:
                session.close()
            self.store.release_dut_lock(dut_name=self.config.dut.name, owner_job_id=self.job_id)

    def execute_smoke_tests(self, *, transition: StateTransition) -> list[SmokeTestResult]:
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
            transition(JobState.TEST_RUNNING, "running smoke tests", None)
            return self.run_smoke_tests(session)
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
                    assertion_failed = (
                        expect is not None
                        and re.search(expect, result.output) is None
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

    def _http_download_and_verify(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
        url: str,
    ) -> None:
        self._check_dut_free_space(session, artifact)
        remote = shlex.quote(self.config.upgrade.remote_path)
        quoted_url = shlex.quote(url)
        session.run_command(
            f"wget -O {remote} {quoted_url}",
            timeout_sec=self.config.upgrade.transfer_timeout_sec,
            cancel_token=self.cancel_token,
        )
        self._verify_remote_firmware(session, artifact)

    def _tftp_download_and_verify(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
        host: str,
    ) -> None:
        self._check_dut_free_space(session, artifact)
        remote = shlex.quote(self.config.upgrade.remote_path)
        filename = shlex.quote(artifact.filename)
        quoted_host = shlex.quote(host)
        session.run_command(
            f"tftp -g -r {filename} -l {remote} {quoted_host}",
            timeout_sec=self.config.upgrade.transfer_timeout_sec,
            cancel_token=self.cancel_token,
        )
        self._verify_remote_firmware(session, artifact)

    def _verify_remote_firmware(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
    ) -> None:
        remote = shlex.quote(self.config.upgrade.remote_path)
        session.run_command(
            f"test $(wc -c < {remote}) -eq {artifact.size_bytes}",
            timeout_sec=self.config.dut.command_timeout_sec,
            cancel_token=self.cancel_token,
        )
        if self.config.upgrade.verify_sha256:
            expected = shlex.quote(artifact.sha256)
            session.run_command(
                f"sha256sum {remote} | grep -i ^{expected}",
                timeout_sec=self.config.dut.command_timeout_sec,
                cancel_token=self.cancel_token,
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
            self.logger.emit(
                level="INFO" if results[-1].passed else "WARN",
                component="tests",
                event="script_test_completed",
                message=(
                    f"script `{script.name}` "
                    f"{'passed' if results[-1].passed else 'failed'} "
                    f"in {results[-1].duration_sec:.2f}s"
                ),
                fields={
                    "name": script.name,
                    "exit_code": results[-1].exit_code,
                    "passed": results[-1].passed,
                    "timed_out": results[-1].timed_out,
                },
            )
        return results

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
        session.connect()
        session.send_newline()
        timeout = self.config.dut.connect_timeout_sec
        login = self.config.dut.login
        prompt_re = re.compile(self.config.dut.prompt)

        if login.password is None:
            session.read_until(
                prompt_re,
                timeout_sec=timeout,
                cancel_token=self.cancel_token,
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
            cancel_token=self.cancel_token,
        )
        if name == "shell":
            return
        if name == "login":
            session.write_command(login.username)
            name, _ = session.read_until_one_of(
                {"shell": sentinels["shell"], "password": sentinels["password"]},
                timeout_sec=timeout,
                cancel_token=self.cancel_token,
            )
            if name == "shell":
                return
        # `name` is now "password" — either we hit it directly, or we sent a
        # username and the device asked for a password.
        session.write_command(login.password, redact_in_transcript=True)
        session.read_until(
            prompt_re,
            timeout_sec=timeout,
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
        serial_path = self.config.dut.serial or self._discover_one_serial_port()
        newline = "\r\n" if self.config.dut.newline == "crlf" else "\n"
        return SerialSession(
            port=serial_path,
            baud=self.config.dut.baud,
            prompt=self.config.dut.prompt,
            transcript_path=self.run_dir / "serial.log",
            newline=newline,
            bytesize=self.config.dut.bytesize,
            parity=self.config.dut.parity,
            stopbits=self.config.dut.stopbits,
        )

    def _discover_one_serial_port(self) -> str:
        ports = discover_serial_ports(self.config.dut.discovery_patterns)
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

    def _firmware_host(self) -> str:
        if self.config.upgrade.transfer == "tftp":
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
