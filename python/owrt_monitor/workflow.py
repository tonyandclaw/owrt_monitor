from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from owrt_monitor.artifacts import ArtifactSelectionError, ExportedArtifact, select_artifact
from owrt_monitor.build_log import classify_build_log
from owrt_monitor.cancel import CancelToken, JobCancelled, with_retry
from owrt_monitor.config import ConfigError, OwrtConfig, load_config
from owrt_monitor.config_diff import diff_configs, summarize
from owrt_monitor.docker_build import DockerBuildClient, DockerBuildError, sha256_file
from owrt_monitor.dut_workflow import DutWorkflow, DutWorkflowError
from owrt_monitor.events import EventLogger
from owrt_monitor.reports import WorkflowReport, write_config_snapshot, write_report
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore

CANCEL_MARKER_NAME = "cancel.flag"

RESUMABLE_FROM = {
    JobState.BUILD_SUCCEEDED,
    JobState.ARTIFACT_SELECTED,
    JobState.ARTIFACT_EXPORTED,
}
NON_RESUMABLE_TERMINAL = {JobState.SUCCEEDED, JobState.DRY_RUN, JobState.CANCELLED}


def cancel_marker_path(run_dir: Path) -> Path:
    return run_dir / CANCEL_MARKER_NAME


def last_progress_state(events_path: Path) -> JobState | None:
    if not events_path.exists():
        return None
    last: str | None = None
    with events_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("event") != "state_transition":
                continue
            state = payload.get("fields", {}).get("state")
            if state:
                last = state
    if last is None:
        return None
    try:
        return JobState(last)
    except ValueError:
        return None


class WorkflowError(RuntimeError):
    """Raised when a workflow cannot complete."""


class BuildWorkflow:
    def __init__(
        self,
        config_path: Path | str,
        *,
        profile: str | None = None,
        docker_client: DockerBuildClient | None = None,
        dut_workflow_kwargs: dict[str, object] | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.profile = profile
        try:
            self.config: OwrtConfig = _load_with_profile(self.config_path, profile)
        except ConfigError:
            raise
        self.artifact_root = self.config.artifact_root(self.config_path)
        self.store = JobStore(self.config.state_db_path(self.config_path))
        self._docker_client_override = docker_client
        self._dut_workflow_kwargs: dict[str, object] = dict(dut_workflow_kwargs or {})

    def _docker_client_for(self, config: OwrtConfig) -> DockerBuildClient:
        if self._docker_client_override is not None:
            return self._docker_client_override
        return DockerBuildClient(config.builder)

    def run(self, *, dry_run: bool = False, allow_flash: bool = False) -> WorkflowReport:
        job_id = _new_job_id()
        run_dir = self.artifact_root / job_id
        run_dir.mkdir(parents=True, exist_ok=True)

        config_snapshot = self.config.redacted_dump()
        write_config_snapshot(run_dir / "config.snapshot.yaml", config_snapshot)

        cancel_token = CancelToken(cancel_marker_path(run_dir))

        self.store.create_job(
            job_id=job_id,
            config_path=self.config_path,
            artifact_dir=run_dir,
            state=JobState.PENDING.value,
            config_snapshot=config_snapshot,
            pid=os.getpid(),
        )
        logger = EventLogger(store=self.store, job_id=job_id, path=run_dir / "events.jsonl")
        report = WorkflowReport(
            job_id=job_id,
            state=JobState.PENDING.value,
            success=False,
            dry_run=dry_run,
            run_dir=run_dir,
        )
        _emit_config_diff_from_last_success(
            self.store, job_id, config_snapshot, logger, report
        )

        builder_lock_acquired = False
        try:
            cancel_token.raise_if_cancelled()
            if not self.store.acquire_builder_lock(
                builder_name=self.config.builder.container,
                owner_job_id=job_id,
                lock_timeout_sec=self.config.builder.lock_timeout_sec,
            ):
                current = self.store.builder_lock_owner(self.config.builder.container)
                raise WorkflowError(
                    f"builder {self.config.builder.container!r} is busy "
                    f"(held by job {current}); wait for it to finish or use a "
                    "different builder.container."
                )
            builder_lock_acquired = True
            docker = self._docker_client_for(self.config)
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
                cancel_token=cancel_token,
                **self._dut_workflow_kwargs,  # type: ignore[arg-type]
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

            cancel_token.raise_if_cancelled()
            docker.preflight()
            cancel_token.raise_if_cancelled()
            self._transition(logger, job_id, JobState.BUILD_RUNNING, "OpenWrt build started")
            try:
                docker.run_build(run_dir / "build.log", cancel_token=cancel_token)
            except DockerBuildError:
                self._attach_build_summary(report, run_dir, logger)
                raise
            cancel_token.raise_if_cancelled()
            self._attach_build_summary(report, run_dir, logger)
            self._attach_build_metadata(report, docker, logger)
            self._transition(logger, job_id, JobState.BUILD_SUCCEEDED, "OpenWrt build succeeded")

            exported = self._select_and_export_artifact(
                docker=docker,
                config=self.config,
                run_dir=run_dir,
                job_id=job_id,
                logger=logger,
                cancel_token=cancel_token,
            )
            report.artifact = exported

            metrics: dict[str, float] = {}
            dut_status: dict[str, object] = {}
            if report.build_summary and report.build_summary.get("duration_sec") is not None:
                metrics["build_duration_sec"] = float(report.build_summary["duration_sec"])
            if allow_flash:
                _assert_artifact_matches_dut(self.config, exported)
                test_results = dut_workflow.execute_upgrade_and_tests(
                    exported,
                    transition=lambda state, message, fields: self._transition(
                        logger,
                        job_id,
                        state,
                        message,
                        fields=fields,
                    ),
                    metrics=metrics,
                    status_out=dut_status,
                )
                report.test_results = [asdict(result) for result in test_results]
            if metrics:
                report.metrics = dict(metrics)
            if dut_status:
                report.dut_status = dict(dut_status)

            report.state = JobState.SUCCEEDED.value
            report.success = True
            self.store.update_job(
                job_id=job_id,
                state=JobState.SUCCEEDED.value,
                result="success",
                metrics=report.metrics,
            )
            logger.emit(
                level="INFO",
                component="workflow",
                event="job_succeeded",
                message="build and artifact export completed",
                fields={"run_dir": str(run_dir), "metrics": metrics},
            )
            write_report(report)
            return report
        except JobCancelled as exc:
            _record_cancellation(report, logger, self.store, job_id, run_dir, exc)
            raise WorkflowError(str(exc)) from exc
        except (
            ArtifactSelectionError,
            DockerBuildError,
            DutWorkflowError,
            ConfigError,
            WorkflowError,
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
        finally:
            if builder_lock_acquired:
                self.store.release_builder_lock(
                    builder_name=self.config.builder.container,
                    owner_job_id=job_id,
                )

    def resume(
        self,
        job_id: str,
        *,
        dry_run: bool = False,
        allow_flash: bool = False,
    ) -> WorkflowReport:
        record = self.store.get_job(job_id)
        if record is None:
            raise WorkflowError(f"no job with id {job_id!r}")

        try:
            recorded_state = JobState(record["state"])
        except ValueError as exc:
            raise WorkflowError(
                f"job {job_id} has unknown state {record['state']!r}"
            ) from exc

        if recorded_state in NON_RESUMABLE_TERMINAL:
            raise WorkflowError(
                f"job {job_id} is in terminal state {recorded_state.value}; cannot resume"
            )

        run_dir = Path(record["artifact_dir"])
        if not run_dir.is_dir():
            raise WorkflowError(f"run directory missing: {run_dir}")

        last = (
            last_progress_state(run_dir / "events.jsonl")
            if recorded_state is JobState.FAILED
            else recorded_state
        )
        if last is None:
            raise WorkflowError(
                f"cannot determine last progress state for job {job_id}; refusing to resume"
            )
        if last not in RESUMABLE_FROM:
            raise WorkflowError(
                f"resume is only supported when last progress state is "
                f"{', '.join(sorted(s.value for s in RESUMABLE_FROM))} "
                f"(was {last.value}); start a fresh run instead"
            )

        try:
            resumed_config: OwrtConfig = OwrtConfig.model_validate(record["config_snapshot"])
        except Exception as exc:
            raise WorkflowError(f"stored config snapshot is invalid: {exc}") from exc

        cancel_token = CancelToken(cancel_marker_path(run_dir))
        cancel_token.clear()
        self.store.update_job_pid(job_id=job_id, pid=os.getpid())

        logger = EventLogger(store=self.store, job_id=job_id, path=run_dir / "events.jsonl")

        skip_export = last is JobState.ARTIFACT_EXPORTED
        existing_artifact: ExportedArtifact | None = None
        if skip_export:
            artifact_record = self.store.get_latest_artifact(job_id)
            if artifact_record is None:
                raise WorkflowError(f"job {job_id} has no recorded artifact; cannot resume")
            existing_artifact = ExportedArtifact(
                container_path=artifact_record["container_path"],
                host_path=Path(artifact_record["host_path"]),
                filename=artifact_record["filename"],
                size_bytes=int(artifact_record["size_bytes"]),
                sha256=artifact_record["sha256"],
            )

        report = WorkflowReport(
            job_id=job_id,
            state=recorded_state.value,
            success=False,
            dry_run=dry_run,
            run_dir=run_dir,
            artifact=existing_artifact,
        )

        logger.emit(
            level="INFO",
            component="workflow",
            event="job_resumed",
            message=f"resuming from {last.value}",
            fields={"resume_from": last.value, "previous_state": recorded_state.value},
        )

        builder_lock_acquired = False
        try:
            cancel_token.raise_if_cancelled()
            if not skip_export:
                if not self.store.acquire_builder_lock(
                    builder_name=resumed_config.builder.container,
                    owner_job_id=job_id,
                    lock_timeout_sec=resumed_config.builder.lock_timeout_sec,
                ):
                    current = self.store.builder_lock_owner(
                        resumed_config.builder.container
                    )
                    raise WorkflowError(
                        f"builder {resumed_config.builder.container!r} is busy "
                        f"(held by job {current}); wait or pick a different builder."
                    )
                builder_lock_acquired = True
            docker = self._docker_client_for(resumed_config)
            dut_workflow = DutWorkflow(
                config=resumed_config,
                run_dir=run_dir,
                logger=logger,
                store=self.store,
                job_id=job_id,
                cancel_token=cancel_token,
                **self._dut_workflow_kwargs,  # type: ignore[arg-type]
            )

            if skip_export:
                report.actions.append(
                    f"Reusing exported artifact: `{existing_artifact.host_path}`"
                )
            else:
                report.actions.append(
                    "Artifact search patterns: "
                    + ", ".join(f"`{p}`" for p in resumed_config.artifact.patterns)
                )
            if allow_flash or (dry_run and skip_export):
                report.actions.extend(dut_workflow.planned_actions(existing_artifact))

            if dry_run:
                report.state = JobState.DRY_RUN.value
                report.success = True
                self.store.update_job(
                    job_id=job_id, state=JobState.DRY_RUN.value, result="dry-run"
                )
                logger.emit(
                    level="INFO",
                    component="workflow",
                    event="resume_dry_run_completed",
                    message="planned resume actions without external side effects",
                    fields={"run_dir": str(run_dir), "resume_from": last.value},
                )
                write_report(report)
                return report

            if skip_export and not allow_flash:
                raise WorkflowError(
                    f"resuming from {last.value} with no flash leaves nothing to do; "
                    "pass --allow-flash or --dry-run"
                )

            self._transition(logger, job_id, JobState.PREFLIGHT, "starting resume preflight")

            if skip_export:
                exported = existing_artifact
                assert exported is not None
            else:
                docker.preflight()
                exported = self._select_and_export_artifact(
                    docker=docker,
                    config=resumed_config,
                    run_dir=run_dir,
                    job_id=job_id,
                    logger=logger,
                    cancel_token=cancel_token,
                )
                report.artifact = exported

            resume_metrics: dict[str, float] = {}
            resume_status: dict[str, object] = {}
            if allow_flash:
                _assert_artifact_matches_dut(resumed_config, exported)
                test_results = dut_workflow.execute_upgrade_and_tests(
                    exported,
                    transition=lambda state, message, fields: self._transition(
                        logger,
                        job_id,
                        state,
                        message,
                        fields=fields,
                    ),
                    metrics=resume_metrics,
                    status_out=resume_status,
                )
                report.test_results = [asdict(result) for result in test_results]
            if resume_metrics:
                report.metrics = dict(resume_metrics)
            if resume_status:
                report.dut_status = dict(resume_status)

            report.state = JobState.SUCCEEDED.value
            report.success = True
            self.store.update_job(
                job_id=job_id,
                state=JobState.SUCCEEDED.value,
                result="success",
                metrics=report.metrics,
            )
            logger.emit(
                level="INFO",
                component="workflow",
                event="resume_succeeded",
                message="resumed job completed",
                fields={"run_dir": str(run_dir), "metrics": resume_metrics},
            )
            write_report(report)
            return report
        except JobCancelled as exc:
            _record_cancellation(report, logger, self.store, job_id, run_dir, exc)
            raise WorkflowError(str(exc)) from exc
        except (
            ArtifactSelectionError,
            DockerBuildError,
            DutWorkflowError,
            WorkflowError,
            OSError,
        ) as exc:
            report.state = JobState.FAILED.value
            report.success = False
            report.warnings.append(str(exc))
            self.store.update_job(job_id=job_id, state=JobState.FAILED.value, result="failed")
            logger.emit(
                level="ERROR",
                component="workflow",
                event="resume_failed",
                message=str(exc),
                fields={"run_dir": str(run_dir)},
            )
            write_report(report)
            raise WorkflowError(str(exc)) from exc
        finally:
            if builder_lock_acquired:
                self.store.release_builder_lock(
                    builder_name=resumed_config.builder.container,
                    owner_job_id=job_id,
                )

    def _attach_build_metadata(
        self,
        report: WorkflowReport,
        docker: DockerBuildClient,
        logger: EventLogger,
    ) -> None:
        command = list(self.config.builder.command)
        make_target: str | None = command[-1] if len(command) > 1 else None
        try:
            git_metadata = docker.gather_build_metadata()
        except Exception as exc:  # gather is best-effort; never let it fail the build
            git_metadata = {}
            logger.emit(
                level="WARN",
                component="build_metadata",
                event="git_metadata_unavailable",
                message=f"could not gather git metadata: {exc}",
                fields={},
            )
        metadata: dict[str, object] = {
            "built_at": datetime.now(UTC).isoformat(),
            "make_target": make_target,
            "profile": self.profile,
            **git_metadata,
        }
        report.build_metadata = metadata
        logger.emit(
            level="INFO",
            component="build_metadata",
            event="build_metadata_captured",
            message="captured build provenance",
            fields=metadata,
        )

    def _attach_build_summary(
        self,
        report: WorkflowReport,
        run_dir: Path,
        logger: EventLogger,
    ) -> None:
        log_path = run_dir / "build.log"
        summary = classify_build_log(log_path)
        report.build_summary = summary.to_dict()
        logger.emit(
            level="INFO" if summary.success else "WARN",
            component="build_log",
            event="build_log_classified",
            message=f"build log classified as {summary.classification}",
            fields=summary.to_dict(),
        )

    def _select_and_export_artifact(
        self,
        *,
        docker: DockerBuildClient,
        config: OwrtConfig,
        run_dir: Path,
        job_id: str,
        logger: EventLogger,
        cancel_token: CancelToken,
    ) -> ExportedArtifact:
        def _select() -> ExportedArtifact:
            candidates = docker.list_artifacts(config.artifact.patterns)
            return select_artifact(
                candidates,
                selection=config.artifact.selection,
                min_size_mb=config.artifact.min_size_mb,
                regex_patterns=config.artifact.regex_patterns,
            )

        selected = with_retry(
            "artifact_select",
            _select,
            policy=config.retry.artifact_select,
            cancel_token=cancel_token,
            logger=logger,
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

        filename = config.artifact.export_filename or selected.filename
        exported = with_retry(
            "artifact_export",
            lambda: docker.copy_artifact(selected, run_dir / "firmware" / filename),
            policy=config.retry.artifact_export,
            cancel_token=cancel_token,
            logger=logger,
        )
        self.store.record_artifact(
            job_id=job_id,
            container_path=exported.container_path,
            host_path=exported.host_path,
            filename=exported.filename,
            size_bytes=exported.size_bytes,
            sha256=exported.sha256,
        )
        self._transition(
            logger,
            job_id,
            JobState.ARTIFACT_EXPORTED,
            "firmware artifact exported to host",
            fields={"host_path": str(exported.host_path), "sha256": exported.sha256},
        )
        return exported

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


def _record_cancellation(
    report: WorkflowReport,
    logger: EventLogger,
    store: JobStore,
    job_id: str,
    run_dir: Path,
    exc: JobCancelled,
) -> None:
    report.state = JobState.CANCELLED.value
    report.success = False
    report.warnings.append(str(exc))
    store.update_job(job_id=job_id, state=JobState.CANCELLED.value, result="cancelled")
    logger.emit(
        level="WARN",
        component="workflow",
        event="job_cancelled",
        message=str(exc),
        fields={"run_dir": str(run_dir)},
    )
    write_report(report)


def _new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


def _load_with_profile(config_path: Path, profile: str | None) -> OwrtConfig:
    config = load_config(config_path)
    if profile is None:
        return config
    return config.with_profile(profile)


def _emit_config_diff_from_last_success(
    store: JobStore,
    job_id: str,
    current_snapshot: dict,
    logger: EventLogger,
    report: WorkflowReport,
) -> None:
    """Compare the about-to-run config against the most-recent successful job's
    snapshot. Emits a `config_diff_from_last_success` event with a summary, and
    appends a one-liner to the report's actions list. Best-effort: silent when
    no prior success exists, never raises.
    """
    try:
        last = store.last_successful_job(exclude_id=job_id)
    except Exception:
        return
    if last is None or not last.get("config_snapshot"):
        return
    changes = diff_configs(last["config_snapshot"], current_snapshot)
    summary = summarize(changes)
    if summary.total == 0:
        report.actions.append(
            f"Config unchanged since last successful job `{last['id']}`."
        )
        return
    report.actions.append(
        f"Config differs from last successful job `{last['id']}`: "
        f"{summary.total} change(s), e.g. {', '.join(c.path for c in summary.sample[:3])}"
    )
    logger.emit(
        level="INFO",
        component="workflow",
        event="config_diff_from_last_success",
        message=f"{summary.total} config field(s) changed since {last['id']}",
        fields={
            "compared_to": last["id"],
            "total_changes": summary.total,
            "sample": [
                {"path": c.path, "old": c.old, "new": c.new} for c in summary.sample
            ],
        },
    )


def _assert_artifact_matches_dut(
    config: OwrtConfig,
    artifact: ExportedArtifact,
) -> None:
    """Verify the firmware filename matches `dut.expected_artifact_pattern`.

    Run-time guard against flashing the wrong board variant — important when
    multiple profiles share a build subdir (e.g. AP and controller both live
    in build/owrt2102/) and a too-broad glob could pick the wrong file.
    No-op when the field is unset.
    """
    pattern = config.dut.expected_artifact_pattern
    if not pattern:
        return
    if re.search(pattern, artifact.filename) is None:
        raise WorkflowError(
            f"refusing to flash DUT {config.dut.name!r}: artifact "
            f"{artifact.filename!r} does not match expected pattern "
            f"{pattern!r}. Tighten artifact.patterns or update "
            "dut.expected_artifact_pattern."
        )


class FlashWorkflow:
    def __init__(self, config_path: Path | str, *, profile: str | None = None) -> None:
        self.config_path = Path(config_path).resolve()
        self.profile = profile
        try:
            self.config: OwrtConfig = _load_with_profile(self.config_path, profile)
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

        cancel_token = CancelToken(cancel_marker_path(run_dir))

        self.store.create_job(
            job_id=job_id,
            config_path=self.config_path,
            artifact_dir=run_dir,
            state=JobState.PENDING.value,
            config_snapshot=config_snapshot,
            pid=os.getpid(),
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
        _emit_config_diff_from_last_success(
            self.store, job_id, config_snapshot, logger, report
        )

        try:
            cancel_token.raise_if_cancelled()
            dut_workflow = DutWorkflow(
                config=self.config,
                run_dir=run_dir,
                logger=logger,
                store=self.store,
                job_id=job_id,
                cancel_token=cancel_token,
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

            cancel_token.raise_if_cancelled()
            self._transition(logger, job_id, JobState.PREFLIGHT, "starting DUT flash preflight")
            _assert_artifact_matches_dut(self.config, artifact)
            flash_metrics: dict[str, float] = {}
            flash_status: dict[str, object] = {}
            test_results = dut_workflow.execute_upgrade_and_tests(
                artifact,
                transition=lambda state, message, fields: self._transition(
                    logger,
                    job_id,
                    state,
                    message,
                    fields=fields,
                ),
                metrics=flash_metrics,
                status_out=flash_status,
            )
            report.test_results = [asdict(result) for result in test_results]
            if flash_metrics:
                report.metrics = dict(flash_metrics)
            if flash_status:
                report.dut_status = dict(flash_status)
            report.state = JobState.SUCCEEDED.value
            report.success = True
            self.store.update_job(
                job_id=job_id,
                state=JobState.SUCCEEDED.value,
                result="success",
                metrics=report.metrics,
            )
            logger.emit(
                level="INFO",
                component="workflow",
                event="flash_succeeded",
                message="DUT flash workflow completed",
                fields={"run_dir": str(run_dir), "artifact": str(firmware_path)},
            )
            write_report(report)
            return report
        except JobCancelled as exc:
            _record_cancellation(report, logger, self.store, job_id, run_dir, exc)
            raise WorkflowError(str(exc)) from exc
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
    def __init__(self, config_path: Path | str, *, profile: str | None = None) -> None:
        self.config_path = Path(config_path).resolve()
        self.profile = profile
        try:
            self.config: OwrtConfig = _load_with_profile(self.config_path, profile)
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

        cancel_token = CancelToken(cancel_marker_path(run_dir))

        self.store.create_job(
            job_id=job_id,
            config_path=self.config_path,
            artifact_dir=run_dir,
            state=JobState.PENDING.value,
            config_snapshot=config_snapshot,
            pid=os.getpid(),
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
            cancel_token.raise_if_cancelled()
            dut_workflow = DutWorkflow(
                config=self.config,
                run_dir=run_dir,
                logger=logger,
                store=self.store,
                job_id=job_id,
                cancel_token=cancel_token,
            )
            report.actions.extend(
                [
                    f"DUT lock: `{self.config.dut.name}`",
                    f"Serial console: `{self.config.dut.serial or '<auto-discover>'}` "
                    f"at `{self.config.dut.baud}` baud",
                ]
            )
            for entry in self.config.tests.smoke:
                if entry.expect:
                    report.actions.append(
                        f"Smoke test: `{entry.command}` (expect /{entry.expect}/)"
                    )
                else:
                    report.actions.append(f"Smoke test: `{entry.command}`")

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

            cancel_token.raise_if_cancelled()
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
        except JobCancelled as exc:
            _record_cancellation(report, logger, self.store, job_id, run_dir, exc)
            raise WorkflowError(str(exc)) from exc
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
