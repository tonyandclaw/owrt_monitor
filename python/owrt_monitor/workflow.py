from __future__ import annotations

import shlex
import uuid
from dataclasses import asdict
from pathlib import Path

from owrt_monitor.artifacts import ArtifactSelectionError, ExportedArtifact, select_artifact
from owrt_monitor.config import ConfigError, OwrtConfig, load_config
from owrt_monitor.docker_build import DockerBuildClient, DockerBuildError, sha256_file
from owrt_monitor.dut_workflow import DutWorkflow, DutWorkflowError
from owrt_monitor.events import EventLogger
from owrt_monitor.reports import WorkflowReport, write_config_snapshot, write_report
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


class WorkflowError(RuntimeError):
    """Raised when a workflow cannot complete."""


class BuildWorkflow:
    def __init__(self, config_path: Path | str) -> None:
        self.config_path = Path(config_path).resolve()
        try:
            self.config: OwrtConfig = load_config(self.config_path)
        except ConfigError:
            raise
        self.artifact_root = self.config.artifact_root(self.config_path)
        self.store = JobStore(self.config.state_db_path(self.config_path))

    def run(self, *, dry_run: bool = False, allow_flash: bool = False) -> WorkflowReport:
        job_id = _new_job_id()
        run_dir = self.artifact_root / job_id
        run_dir.mkdir(parents=True, exist_ok=True)

        config_snapshot = self.config.redacted_dump()
        write_config_snapshot(run_dir / "config.snapshot.yaml", config_snapshot)

        self.store.create_job(
            job_id=job_id,
            config_path=self.config_path,
            artifact_dir=run_dir,
            state=JobState.PENDING.value,
            config_snapshot=config_snapshot,
        )
        logger = EventLogger(store=self.store, job_id=job_id, path=run_dir / "events.jsonl")
        report = WorkflowReport(
            job_id=job_id,
            state=JobState.PENDING.value,
            success=False,
            dry_run=dry_run,
            run_dir=run_dir,
        )

        try:
            docker = DockerBuildClient(self.config.builder)
            self._transition(logger, job_id, JobState.PREFLIGHT, "starting preflight checks")

            build_command = " ".join(
                shlex.quote(part) for part in docker.build_command(redact_env=True)
            )
            report.actions.append(f"Build command: `{build_command}`")
            report.actions.append(
                "Artifact search patterns: "
                + ", ".join(f"`{pattern}`" for pattern in self.config.artifact.patterns)
            )
            dut_workflow = DutWorkflow(
                config=self.config,
                run_dir=run_dir,
                logger=logger,
                store=self.store,
                job_id=job_id,
            )
            if allow_flash:
                report.actions.extend(dut_workflow.planned_actions())

            if dry_run:
                report.state = JobState.DRY_RUN.value
                report.success = True
                self.store.update_job(job_id=job_id, state=JobState.DRY_RUN.value, result="dry-run")
                logger.emit(
                    level="INFO",
                    component="workflow",
                    event="dry_run_completed",
                    message="validated config and planned actions without external side effects",
                    fields={"run_dir": str(run_dir)},
                )
                write_report(report)
                return report

            docker.preflight()
            self._transition(logger, job_id, JobState.BUILD_RUNNING, "OpenWrt build started")
            docker.run_build(run_dir / "build.log")
            self._transition(logger, job_id, JobState.BUILD_SUCCEEDED, "OpenWrt build succeeded")

            candidates = docker.list_artifacts(self.config.artifact.patterns)
            selected = select_artifact(
                candidates,
                selection=self.config.artifact.selection,
                min_size_mb=self.config.artifact.min_size_mb,
            )
            self._transition(
                logger,
                job_id,
                JobState.ARTIFACT_SELECTED,
                "firmware artifact selected",
                fields={
                    "path": selected.path,
                    "size_bytes": selected.size_bytes,
                    "mtime": selected.mtime,
                },
            )

            filename = self.config.artifact.export_filename or selected.filename
            exported = docker.copy_artifact(selected, run_dir / "firmware" / filename)
            self.store.record_artifact(
                job_id=job_id,
                container_path=exported.container_path,
                host_path=exported.host_path,
                filename=exported.filename,
                size_bytes=exported.size_bytes,
                sha256=exported.sha256,
            )
            report.artifact = exported
            self._transition(
                logger,
                job_id,
                JobState.ARTIFACT_EXPORTED,
                "firmware artifact exported to host",
                fields={"host_path": str(exported.host_path), "sha256": exported.sha256},
            )

            if allow_flash:
                test_results = dut_workflow.execute_upgrade_and_tests(
                    exported,
                    transition=lambda state, message, fields: self._transition(
                        logger,
                        job_id,
                        state,
                        message,
                        fields=fields,
                    ),
                )
                report.test_results = [asdict(result) for result in test_results]

            report.state = JobState.SUCCEEDED.value
            report.success = True
            self.store.update_job(job_id=job_id, state=JobState.SUCCEEDED.value, result="success")
            logger.emit(
                level="INFO",
                component="workflow",
                event="job_succeeded",
                message="build and artifact export completed",
                fields={"run_dir": str(run_dir)},
            )
            write_report(report)
            return report
        except (
            ArtifactSelectionError,
            DockerBuildError,
            DutWorkflowError,
            ConfigError,
            OSError,
        ) as exc:
            report.state = JobState.FAILED.value
            report.success = False
            report.warnings.append(str(exc))
            self.store.update_job(job_id=job_id, state=JobState.FAILED.value, result="failed")
            logger.emit(
                level="ERROR",
                component="workflow",
                event="job_failed",
                message=str(exc),
                fields={"run_dir": str(run_dir)},
            )
            write_report(report)
            raise WorkflowError(str(exc)) from exc

    def _transition(
        self,
        logger: EventLogger,
        job_id: str,
        state: JobState,
        message: str,
        *,
        fields: dict[str, object] | None = None,
    ) -> None:
        self.store.update_job(job_id=job_id, state=state.value)
        logger.emit(
            level="INFO",
            component="workflow",
            event="state_transition",
            message=message,
            fields={"state": state.value, **(fields or {})},
        )


def _new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


class FlashWorkflow:
    def __init__(self, config_path: Path | str) -> None:
        self.config_path = Path(config_path).resolve()
        try:
            self.config: OwrtConfig = load_config(self.config_path)
        except ConfigError:
            raise
        self.artifact_root = self.config.artifact_root(self.config_path)
        self.store = JobStore(self.config.state_db_path(self.config_path))

    def run(
        self,
        *,
        artifact_path: Path | str,
        dry_run: bool = False,
        allow_flash: bool = False,
    ) -> WorkflowReport:
        firmware_path = Path(artifact_path).resolve()
        if not firmware_path.is_file():
            raise WorkflowError(f"firmware artifact does not exist: {firmware_path}")

        job_id = _new_job_id()
        run_dir = self.artifact_root / job_id
        run_dir.mkdir(parents=True, exist_ok=True)

        config_snapshot = self.config.redacted_dump()
        write_config_snapshot(run_dir / "config.snapshot.yaml", config_snapshot)

        self.store.create_job(
            job_id=job_id,
            config_path=self.config_path,
            artifact_dir=run_dir,
            state=JobState.PENDING.value,
            config_snapshot=config_snapshot,
        )
        logger = EventLogger(store=self.store, job_id=job_id, path=run_dir / "events.jsonl")
        artifact = ExportedArtifact(
            container_path="<host>",
            host_path=firmware_path,
            filename=firmware_path.name,
            size_bytes=firmware_path.stat().st_size,
            sha256=sha256_file(firmware_path),
        )
        report = WorkflowReport(
            job_id=job_id,
            state=JobState.PENDING.value,
            success=False,
            dry_run=dry_run,
            run_dir=run_dir,
            artifact=artifact,
        )

        try:
            dut_workflow = DutWorkflow(
                config=self.config,
                run_dir=run_dir,
                logger=logger,
                store=self.store,
                job_id=job_id,
            )
            report.actions.extend(dut_workflow.planned_actions(artifact))

            if dry_run:
                report.state = JobState.DRY_RUN.value
                report.success = True
                self.store.update_job(job_id=job_id, state=JobState.DRY_RUN.value, result="dry-run")
                logger.emit(
                    level="INFO",
                    component="workflow",
                    event="flash_dry_run_completed",
                    message="planned DUT flash actions without external side effects",
                    fields={"run_dir": str(run_dir), "artifact": str(firmware_path)},
                )
                write_report(report)
                return report

            if not allow_flash:
                raise WorkflowError("flashing a DUT requires --allow-flash")

            self._transition(logger, job_id, JobState.PREFLIGHT, "starting DUT flash preflight")
            test_results = dut_workflow.execute_upgrade_and_tests(
                artifact,
                transition=lambda state, message, fields: self._transition(
                    logger,
                    job_id,
                    state,
                    message,
                    fields=fields,
                ),
            )
            report.test_results = [asdict(result) for result in test_results]
            report.state = JobState.SUCCEEDED.value
            report.success = True
            self.store.update_job(job_id=job_id, state=JobState.SUCCEEDED.value, result="success")
            logger.emit(
                level="INFO",
                component="workflow",
                event="flash_succeeded",
                message="DUT flash workflow completed",
                fields={"run_dir": str(run_dir), "artifact": str(firmware_path)},
            )
            write_report(report)
            return report
        except (DutWorkflowError, WorkflowError, OSError) as exc:
            report.state = JobState.FAILED.value
            report.success = False
            report.warnings.append(str(exc))
            self.store.update_job(job_id=job_id, state=JobState.FAILED.value, result="failed")
            logger.emit(
                level="ERROR",
                component="workflow",
                event="flash_failed",
                message=str(exc),
                fields={"run_dir": str(run_dir), "artifact": str(firmware_path)},
            )
            write_report(report)
            raise WorkflowError(str(exc)) from exc

    def _transition(
        self,
        logger: EventLogger,
        job_id: str,
        state: JobState,
        message: str,
        *,
        fields: dict[str, object] | None = None,
    ) -> None:
        self.store.update_job(job_id=job_id, state=state.value)
        logger.emit(
            level="INFO",
            component="workflow",
            event="state_transition",
            message=message,
            fields={"state": state.value, **(fields or {})},
        )


class SmokeTestWorkflow:
    def __init__(self, config_path: Path | str) -> None:
        self.config_path = Path(config_path).resolve()
        try:
            self.config: OwrtConfig = load_config(self.config_path)
        except ConfigError:
            raise
        self.artifact_root = self.config.artifact_root(self.config_path)
        self.store = JobStore(self.config.state_db_path(self.config_path))

    def run(self, *, dry_run: bool = False) -> WorkflowReport:
        job_id = _new_job_id()
        run_dir = self.artifact_root / job_id
        run_dir.mkdir(parents=True, exist_ok=True)

        config_snapshot = self.config.redacted_dump()
        write_config_snapshot(run_dir / "config.snapshot.yaml", config_snapshot)

        self.store.create_job(
            job_id=job_id,
            config_path=self.config_path,
            artifact_dir=run_dir,
            state=JobState.PENDING.value,
            config_snapshot=config_snapshot,
        )
        logger = EventLogger(store=self.store, job_id=job_id, path=run_dir / "events.jsonl")
        report = WorkflowReport(
            job_id=job_id,
            state=JobState.PENDING.value,
            success=False,
            dry_run=dry_run,
            run_dir=run_dir,
        )

        try:
            dut_workflow = DutWorkflow(
                config=self.config,
                run_dir=run_dir,
                logger=logger,
                store=self.store,
                job_id=job_id,
            )
            report.actions.extend(
                [
                    f"DUT lock: `{self.config.dut.name}`",
                    f"Serial console: `{self.config.dut.serial or '<auto-discover>'}` "
                    f"at `{self.config.dut.baud}` baud",
                ]
            )
            report.actions.extend(f"Smoke test: `{command}`" for command in self.config.tests.smoke)

            if dry_run:
                report.state = JobState.DRY_RUN.value
                report.success = True
                self.store.update_job(job_id=job_id, state=JobState.DRY_RUN.value, result="dry-run")
                logger.emit(
                    level="INFO",
                    component="workflow",
                    event="test_dry_run_completed",
                    message="planned DUT smoke tests without external side effects",
                    fields={"run_dir": str(run_dir)},
                )
                write_report(report)
                return report

            self._transition(
                logger,
                job_id,
                JobState.PREFLIGHT,
                "starting DUT smoke test preflight",
            )
            test_results = dut_workflow.execute_smoke_tests(
                transition=lambda state, message, fields: self._transition(
                    logger,
                    job_id,
                    state,
                    message,
                    fields=fields,
                ),
            )
            report.test_results = [asdict(result) for result in test_results]
            report.state = (
                JobState.SUCCEEDED.value
                if all(result.passed for result in test_results)
                else JobState.FAILED.value
            )
            report.success = all(result.passed for result in test_results)
            self.store.update_job(
                job_id=job_id,
                state=report.state,
                result="success" if report.success else "failed",
            )
            logger.emit(
                level="INFO" if report.success else "ERROR",
                component="workflow",
                event="smoke_tests_completed",
                message="DUT smoke tests completed",
                fields={"run_dir": str(run_dir), "success": report.success},
            )
            write_report(report)
            if not report.success:
                raise WorkflowError("one or more smoke tests failed")
            return report
        except (DutWorkflowError, WorkflowError, OSError) as exc:
            if not report.warnings:
                report.warnings.append(str(exc))
            if report.state != JobState.FAILED.value:
                report.state = JobState.FAILED.value
                report.success = False
                self.store.update_job(job_id=job_id, state=JobState.FAILED.value, result="failed")
                logger.emit(
                    level="ERROR",
                    component="workflow",
                    event="smoke_tests_failed",
                    message=str(exc),
                    fields={"run_dir": str(run_dir)},
                )
                write_report(report)
            raise WorkflowError(str(exc)) from exc

    def _transition(
        self,
        logger: EventLogger,
        job_id: str,
        state: JobState,
        message: str,
        *,
        fields: dict[str, object] | None = None,
    ) -> None:
        self.store.update_job(job_id=job_id, state=state.value)
        logger.emit(
            level="INFO",
            component="workflow",
            event="state_transition",
            message=message,
            fields={"state": state.value, **(fields or {})},
        )
