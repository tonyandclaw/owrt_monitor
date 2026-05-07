from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from owrt_monitor.artifacts import ExportedArtifact
from owrt_monitor.config import OwrtConfig
from owrt_monitor.dut_serial import SerialError, SerialSession, discover_serial_ports
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


@dataclass(frozen=True)
class SmokeTestResult:
    command: str
    passed: bool
    output: str
    duration_sec: float


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
    ) -> None:
        self.config = config
        self.run_dir = run_dir
        self.logger = logger
        self.store = store
        self.job_id = job_id
        self._serial_session = serial_session

    def planned_actions(self, artifact: ExportedArtifact | None = None) -> list[str]:
        serial = self.config.dut.serial or "<auto-discover>"
        host = self.config.upgrade.http_host or "<host-ip>"
        filename = artifact.filename if artifact is not None else "<firmware>"
        url = f"http://{host}:<port>/{filename}"
        remote_path = shlex.quote(self.config.upgrade.remote_path)
        actions = [
            f"DUT lock: `{self.config.dut.name}`",
            f"Serial console: `{serial}` at `{self.config.dut.baud}` baud",
            f"Firmware transfer: `wget -O {remote_path} {shlex.quote(url)}`",
            f"Upgrade command: `{self.config.upgrade.command}`",
        ]
        actions.extend(f"Smoke test: `{command}`" for command in self.config.tests.smoke)
        return actions

    def execute_upgrade_and_tests(
        self,
        artifact: ExportedArtifact,
        *,
        transition: StateTransition,
    ) -> list[SmokeTestResult]:
        if self.config.upgrade.transfer != "http":
            raise DutWorkflowError(
                f"transfer method {self.config.upgrade.transfer!r} is not implemented yet"
            )

        if not self.store.acquire_dut_lock(
            dut_name=self.config.dut.name,
            owner_job_id=self.job_id,
        ):
            raise DutWorkflowError(f"DUT {self.config.dut.name} is already locked")

        transition(JobState.DUT_LOCKED, "DUT lock acquired", {"dut": self.config.dut.name})

        session: SerialSession | None = None
        server: TemporaryFirmwareServer | None = None

        try:
            session = self._serial_session or self._create_serial_session()
            server = TemporaryFirmwareServer(
                directory=artifact.host_path.parent,
                bind=self.config.upgrade.http_bind,
                port=self.config.upgrade.http_port,
            )
            session.connect()
            session.send_newline()
            session.read_until_prompt(timeout_sec=self.config.dut.connect_timeout_sec)
            transition(
                JobState.DUT_READY,
                "DUT serial prompt is ready",
                {"serial": self.config.dut.serial},
            )

            server.start()
            host = self._firmware_host()
            url = server.url_for(artifact.filename, host=host)
            self._download_and_verify(session, artifact, url)
            transition(
                JobState.FIRMWARE_TRANSFERRED,
                "firmware transferred to DUT",
                {"url": url, "remote_path": self.config.upgrade.remote_path},
            )

            transition(
                JobState.UPGRADE_RUNNING,
                "starting configured firmware upgrade command",
                {"command": self.config.upgrade.command},
            )
            session.write_command(self.config.upgrade.command)
            transition(JobState.REBOOT_WAIT, "waiting for DUT to return after upgrade", None)
            session.read_until(
                re.compile(self.config.dut.prompt),
                timeout_sec=self.config.upgrade.boot_timeout_sec,
            )
            transition(JobState.DUT_ONLINE, "DUT prompt returned after upgrade", None)

            transition(JobState.TEST_RUNNING, "running smoke tests", None)
            return self.run_smoke_tests(session)
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
        ):
            raise DutWorkflowError(f"DUT {self.config.dut.name} is already locked")

        transition(JobState.DUT_LOCKED, "DUT lock acquired", {"dut": self.config.dut.name})
        session: SerialSession | None = None

        try:
            session = self._serial_session or self._create_serial_session()
            session.connect()
            session.send_newline()
            session.read_until_prompt(timeout_sec=self.config.dut.connect_timeout_sec)
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
                active_session.connect()
                active_session.send_newline()
                active_session.read_until_prompt(timeout_sec=self.config.dut.connect_timeout_sec)

            for command in self.config.tests.smoke:
                started = time.monotonic()
                try:
                    result = active_session.run_command(
                        command,
                        timeout_sec=self.config.tests.command_timeout_sec,
                    )
                    test_result = SmokeTestResult(
                        command=command,
                        passed=True,
                        output=result.output,
                        duration_sec=result.duration_sec,
                    )
                except SerialError as exc:
                    test_result = SmokeTestResult(
                        command=command,
                        passed=False,
                        output=str(exc),
                        duration_sec=time.monotonic() - started,
                    )

                results.append(test_result)
                self.store.record_test_result(job_id=self.job_id, **asdict(test_result))

            return results
        finally:
            if close_when_done:
                active_session.close()

    def _download_and_verify(
        self,
        session: SerialSession,
        artifact: ExportedArtifact,
        url: str,
    ) -> None:
        remote = shlex.quote(self.config.upgrade.remote_path)
        quoted_url = shlex.quote(url)
        session.run_command(
            f"wget -O {remote} {quoted_url}",
            timeout_sec=self.config.upgrade.transfer_timeout_sec,
        )
        session.run_command(
            f"test $(wc -c < {remote}) -eq {artifact.size_bytes}",
            timeout_sec=self.config.dut.command_timeout_sec,
        )

        if self.config.upgrade.verify_sha256:
            expected = shlex.quote(artifact.sha256)
            session.run_command(
                f"sha256sum {remote} | grep -i ^{expected}",
                timeout_sec=self.config.dut.command_timeout_sec,
            )

    def _create_serial_session(self) -> SerialSession:
        serial_path = self.config.dut.serial or self._discover_one_serial_port()
        newline = "\r\n" if self.config.dut.newline == "crlf" else "\n"
        return SerialSession(
            port=serial_path,
            baud=self.config.dut.baud,
            prompt=self.config.dut.prompt,
            transcript_path=self.run_dir / "serial.log",
            newline=newline,
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
        configured = self.config.upgrade.http_host
        if configured:
            return configured
        inferred = infer_host_for_target(self.config.dut.network.address)
        if inferred:
            return inferred
        raise DutWorkflowError(
            "upgrade.http_host is required when the host IP cannot be inferred from dut.network"
        )
