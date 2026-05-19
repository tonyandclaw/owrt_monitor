from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fake_docker import FakeDockerBuildClient, FakeFirmwareServer
from owrt_monitor.cancel import CancelToken
from owrt_monitor.config import BuilderConfig, load_config
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.dut_workflow import DutWorkflowError
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import (
    BuildWorkflow,
    WorkflowError,
    cancel_marker_path,
)


class _FakeSerialTransport:
    """Returns canned chunks for each successive read; writes go to a list."""

    def __init__(self, chunks: list[bytes | BaseException]) -> None:
        self.chunks = list(chunks)
        self.writes: list[bytes] = []
        self.closed = False

    @property
    def in_waiting(self) -> int:
        if not self.chunks:
            return 0
        chunk = self.chunks[0]
        return 1 if isinstance(chunk, BaseException) else len(chunk)

    def read(self, size: int = 1) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


def _write_config(tmp_path: Path, **overrides: dict) -> Path:
    raw = {
        "project": {
            "name": "integration",
            "artifact_dir": str(tmp_path / "artifacts"),
        },
        "builder": {
            "container": "fake-builder",
            "workdir": "/work/openwrt",
            "command": ["make", "fake.profile"],
            "env": {"FORCE_UNSAFE_CONFIGURE": "1"},
        },
        "artifact": {
            "patterns": ["build/fake/bin/target/openwrt-*-sysupgrade.bin"],
            "selection": "newest",
            "min_size_mb": 0,
        },
        "dut": {"serial": "/dev/fake"},
    }
    raw.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _builder_from_config(config_path: Path) -> BuilderConfig:
    return load_config(config_path).builder


def test_build_workflow_end_to_end_happy_path(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)

    report = workflow.run(dry_run=False, allow_flash=False)

    # Workflow result
    assert report.success is True
    assert report.state == JobState.SUCCEEDED.value
    assert report.dry_run is False

    # Artifact wiring
    assert report.artifact is not None
    assert report.artifact.host_path.exists()
    assert report.artifact.size_bytes == len(fake.artifact_payload)
    assert report.artifact.host_path.read_bytes() == fake.artifact_payload

    # Build log classifier ran and surfaced the success duration
    assert report.build_summary is not None
    assert report.build_summary["classification"] == "success"
    assert report.build_summary["duration_sec"] == pytest.approx(83.456, abs=0.01)

    # Fake recorded the expected sequence of calls
    assert fake.preflight_calls == 1
    assert fake.run_build_calls == 1
    assert fake.list_artifacts_calls == [
        ["build/fake/bin/target/openwrt-*-sysupgrade.bin"]
    ]
    assert len(fake.copy_artifact_calls) == 1

    # Persisted job state
    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(report.job_id)
    assert record is not None
    assert record["state"] == JobState.SUCCEEDED.value
    assert record["result"] == "success"

    # Run dir contents on disk
    assert (report.run_dir / "build.log").exists()
    assert (report.run_dir / "events.jsonl").exists()
    assert (report.run_dir / "config.snapshot.yaml").exists()
    report_md = (report.run_dir / "report.md").read_text(encoding="utf-8")
    assert "Classification: `success`" in report_md
    assert "SUCCEEDED" in report_md


def test_build_workflow_captures_provenance_metadata(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, profile=None, docker_client=fake)

    report = workflow.run(dry_run=False, allow_flash=False)

    assert fake.gather_build_metadata_calls == 1
    assert report.build_metadata is not None
    md = report.build_metadata
    assert md["git_commit"] == "abc1234deadbeef5678"
    assert md["git_describe"] == "abc1234-dirty"
    assert md["git_dirty"] is True
    # Make target derived from builder.command's tail.
    assert md["make_target"] == "fake.profile"
    assert md["profile"] is None
    assert md["built_at"] is not None  # ISO timestamp string

    # Rendered into report.md as a Provenance section.
    report_md = (report.run_dir / "report.md").read_text(encoding="utf-8")
    assert "## Provenance" in report_md
    assert "git_commit: `abc1234deadbeef5678`" in report_md
    assert "make_target: `fake.profile`" in report_md


def test_build_workflow_metadata_records_active_profile(tmp_path: Path) -> None:
    raw = yaml.safe_load(_write_config(tmp_path).read_text(encoding="utf-8"))
    raw["profiles"] = {
        "ap": {"builder": {"command": ["make", "owrt2102.asus_mt_wifi7_mt7987"]}}
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    fake = FakeDockerBuildClient(builder=load_config(config_path).with_profile("ap").builder)
    workflow = BuildWorkflow(config_path, profile="ap", docker_client=fake)
    report = workflow.run(dry_run=False, allow_flash=False)

    assert report.build_metadata is not None
    assert report.build_metadata["profile"] == "ap"
    assert report.build_metadata["make_target"] == "owrt2102.asus_mt_wifi7_mt7987"


def test_build_workflow_tolerates_metadata_gather_failure(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))

    def _raises() -> dict[str, object]:
        raise RuntimeError("simulated git failure")

    fake.gather_build_metadata = _raises  # type: ignore[assignment]

    workflow = BuildWorkflow(config_path, docker_client=fake)
    # Build still succeeds — provenance is best-effort.
    report = workflow.run(dry_run=False, allow_flash=False)

    assert report.success is True
    assert report.build_metadata is not None
    # No git_* keys but built_at / make_target / profile still present.
    assert "git_commit" not in report.build_metadata
    assert report.build_metadata["make_target"] == "fake.profile"


def test_build_workflow_refuses_when_builder_lock_held(tmp_path: Path) -> None:
    """Concurrent build prevention: if another job holds the builder lock,
    the new run should fail-fast with FAILED state and no docker calls."""
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)

    # Pre-populate a lock owned by a different "job".
    workflow.store.acquire_builder_lock(
        builder_name=workflow.config.builder.container,
        owner_job_id="someone_else",
    )

    with pytest.raises(WorkflowError, match=r"is busy.*someone_else"):
        workflow.run(dry_run=False, allow_flash=False)

    # No docker calls should have happened.
    assert fake.preflight_calls == 0
    assert fake.run_build_calls == 0

    # State recorded as FAILED.
    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(_only_job_id(state_db))
    assert record is not None
    assert record["state"] == JobState.FAILED.value
    assert record["result"] == "failed"


def test_build_workflow_releases_builder_lock_on_success(tmp_path: Path) -> None:
    """A clean build leaves no lock behind, so the next run can proceed."""
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)

    workflow.run(dry_run=False, allow_flash=False)
    assert workflow.store.builder_lock_owner(workflow.config.builder.container) is None


def test_build_workflow_releases_builder_lock_on_failure(tmp_path: Path) -> None:
    """Even on build failure the lock must release — finally must run."""
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(
        builder=_builder_from_config(config_path),
        build_should_fail=True,
    )
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError):
        workflow.run(dry_run=False, allow_flash=False)
    assert workflow.store.builder_lock_owner(workflow.config.builder.container) is None


def test_build_workflow_handles_preflight_failure_cleanly(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(
        builder=_builder_from_config(config_path),
        preflight_should_fail=True,
        preflight_failure_message="insufficient disk in fake-builder:/work: 200 MB free",
    )
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError, match=r"insufficient disk"):
        workflow.run(dry_run=False, allow_flash=False)

    # Build was never attempted, FAILED state is recorded.
    assert fake.run_build_calls == 0
    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(_only_job_id(state_db))
    assert record is not None
    assert record["state"] == JobState.FAILED.value
    assert record["result"] == "failed"


def test_run_with_flash_checks_serial_before_build_preflight(monkeypatch, tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))

    def fail_serial_preflight(self):
        raise DutWorkflowError("serial console is not interactive on /dev/fake")

    monkeypatch.setattr(
        "owrt_monitor.dut_workflow.DutWorkflow.preflight_serial_interactive",
        fail_serial_preflight,
    )
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError, match=r"serial console is not interactive"):
        workflow.run(dry_run=False, allow_flash=True)

    assert fake.preflight_calls == 0
    assert fake.run_build_calls == 0


def test_build_workflow_classifies_disk_full_failure(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(
        builder=_builder_from_config(config_path),
        build_should_fail=True,
    )
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError):
        workflow.run(dry_run=False, allow_flash=False)

    # Even on failure the build log should be classified and attached for diagnostics.
    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(_only_job_id(state_db))
    assert record is not None
    assert record["state"] == JobState.FAILED.value
    assert record["result"] == "failed"

    run_dir = Path(record["artifact_dir"])
    report_md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Classification: `disk_full`" in report_md
    assert "No space left on device" in report_md


def test_build_workflow_handles_build_timeout(tmp_path: Path) -> None:
    raw = yaml.safe_load(_write_config(tmp_path).read_text(encoding="utf-8"))
    raw["builder"]["timeout_sec"] = 5  # express the intent in config
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    fake = FakeDockerBuildClient(
        builder=_builder_from_config(config_path),
        build_should_timeout=True,
    )
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError, match=r"build timed out"):
        workflow.run(dry_run=False, allow_flash=False)

    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(_only_job_id(state_db))
    assert record is not None
    assert record["state"] == JobState.FAILED.value
    # build_summary should still attach so the partial log is preserved for triage.
    run_dir = Path(record["artifact_dir"])
    report_md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Classification:" in report_md  # classifier ran on partial output


def test_build_workflow_respects_cancel_request_mid_build(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(
        builder=_builder_from_config(config_path),
        cancel_during_build=True,  # the fake will set the cancel marker during run_build
    )
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError, match="cancel"):
        workflow.run(dry_run=False, allow_flash=False)

    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(_only_job_id(state_db))
    assert record is not None
    assert record["state"] == JobState.CANCELLED.value
    assert record["result"] == "cancelled"


def test_build_workflow_full_flow_with_allow_flash(tmp_path: Path) -> None:
    """End-to-end: BuildWorkflow.run(allow_flash=True) drives every state transition
    from PENDING through SUCCEEDED using only fakes — no docker, no real serial,
    no real HTTP server. Locks in the wiring between BuildWorkflow / DutWorkflow /
    DockerBuildClient / SerialSession / TemporaryFirmwareServer."""
    raw = yaml.safe_load(_write_config(tmp_path).read_text(encoding="utf-8"))
    raw["upgrade"] = {
        "transfer": "http",
        "remote_path": "/tmp/firmware.bin",
        "command": "sysupgrade -n /tmp/firmware.bin",
        "boot_timeout_sec": 5,
        "transfer_timeout_sec": 5,
        "http_host": "127.0.0.1",
        "verify_sha256": True,
    }
    raw["dut"] = {
        "name": "dut-int",
        "serial": "/dev/fake",
        "prompt": r"root@OpenWrt:.*# ",
        "connect_timeout_sec": 1,
        "command_timeout_sec": 1,
    }
    raw["tests"] = {
        "smoke": ["ubus call system board"],
        "command_timeout_sec": 1,
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)

    fake_docker = FakeDockerBuildClient(builder=config.builder)
    fake_server = FakeFirmwareServer(port=8888)

    prompt = b"root@OpenWrt:/# "
    status_json = (
        b'{"kernel":"5.15.0","hostname":"OpenWrt","board_name":"mt7987",'
        b'"release":{"distribution":"OpenWrt","version":"22.03"}}\n'
    )
    transport = _FakeSerialTransport(
        [
            prompt,                       # initial read_until_prompt after connect
            b"download ok\n" + prompt,    # wget -O /tmp/firmware.bin <url>
            b"size ok\n" + prompt,        # test $(wc -c) == size
            b"sha ok\n" + prompt,         # sha256sum | grep
            b"rebooting\n" + prompt,      # sysupgrade reboot wait
            status_json + prompt,         # ubus call system board (status capture)
            b"board ok\n" + prompt,       # smoke test ubus call
        ]
    )
    fake_session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )

    workflow = BuildWorkflow(
        config_path,
        docker_client=fake_docker,
        dut_workflow_kwargs={
            "serial_session": fake_session,
            "firmware_server": fake_server,
        },
    )
    report = workflow.run(dry_run=False, allow_flash=True)

    # Workflow result + report shape
    assert report.success is True
    assert report.state == JobState.SUCCEEDED.value
    assert len(report.test_results) == 1
    assert report.test_results[0]["passed"] is True
    assert report.artifact is not None

    # The fake server lifecycle was driven correctly.
    assert fake_server.started is True
    assert fake_server.stopped is True

    # The serial session saw exactly the commands we expect, in order.
    written = b"".join(transport.writes)
    assert b"wget -O" in written
    assert b"sha256sum" in written
    assert b"sysupgrade -n" in written
    assert b"ubus call system board" in written

    # State machine reached every expected transition.
    state_db = workflow.config.state_db_path(config_path.resolve())
    import sqlite3

    with sqlite3.connect(state_db) as conn:
        events = [
            row[0]
            for row in conn.execute(
                "SELECT json_extract(fields, '$.state') "
                "FROM job_events WHERE event = 'state_transition' "
                "ORDER BY id"
            ).fetchall()
        ]
    expected_states = [
        "PREFLIGHT",
        "BUILD_RUNNING",
        "BUILD_SUCCEEDED",
        "ARTIFACT_SELECTED",
        "ARTIFACT_EXPORTED",
        "DUT_LOCKED",
        "DUT_READY",
        "FIRMWARE_TRANSFERRED",
        "UPGRADE_RUNNING",
        "REBOOT_WAIT",
        "DUT_ONLINE",
        "TEST_RUNNING",
    ]
    for state in expected_states:
        assert state in events, f"state {state} missing from event stream {events}"

    # DUT lock released cleanly so a follow-up job can acquire it.
    assert workflow.store.acquire_dut_lock(
        dut_name=config.dut.name, owner_job_id="next_job"
    ) is True


def test_build_workflow_reconnects_serial_during_reboot_wait(tmp_path: Path) -> None:
    """End-to-end reboot wait should tolerate a USB serial drop and keep going."""
    raw = yaml.safe_load(_write_config(tmp_path).read_text(encoding="utf-8"))
    raw["upgrade"] = {
        "transfer": "http",
        "remote_path": "/tmp/firmware.bin",
        "command": "sysupgrade -n /tmp/firmware.bin",
        "boot_timeout_sec": 5,
        "transfer_timeout_sec": 5,
        "http_host": "127.0.0.1",
        "verify_sha256": True,
    }
    raw["dut"] = {
        "name": "dut-int",
        "serial": "/dev/fake",
        "prompt": r"root@OpenWrt:.*# ",
        "connect_timeout_sec": 1,
        "command_timeout_sec": 1,
    }
    raw["tests"] = {
        "smoke": ["ubus call system board"],
        "command_timeout_sec": 1,
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)

    prompt = b"root@OpenWrt:/# "
    status_json = (
        b'{"kernel":"5.15.0","hostname":"OpenWrt","board_name":"mt7987",'
        b'"release":{"distribution":"OpenWrt","version":"22.03"}}\n'
    )
    first_transport = _FakeSerialTransport(
        [
            prompt,
            b"download ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            OSError("serial device disappeared during reboot"),
        ]
    )
    second_transport = _FakeSerialTransport(
        [
            b"rebooted after reconnect\n" + prompt,
            status_json + prompt,
            b"board ok\n" + prompt,
        ]
    )
    transports = [first_transport, second_transport]

    def transport_factory() -> _FakeSerialTransport:
        return transports.pop(0)

    fake_session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=tmp_path / "serial.log",
        transport_factory=transport_factory,
    )

    workflow = BuildWorkflow(
        config_path,
        docker_client=FakeDockerBuildClient(builder=config.builder),
        dut_workflow_kwargs={
            "serial_session": fake_session,
            "firmware_server": FakeFirmwareServer(port=8888),
        },
    )
    report = workflow.run(dry_run=False, allow_flash=True)

    assert report.success is True
    assert report.state == JobState.SUCCEEDED.value
    assert first_transport.closed is True
    assert b"sysupgrade -n" in b"".join(first_transport.writes)
    assert second_transport.writes[0] == b"\n"
    transcript = (tmp_path / "serial.log").read_text(encoding="utf-8")
    assert "serial I/O error" in transcript
    assert "serial reconnected" in transcript


def test_build_workflow_fails_when_post_upgrade_smoke_fails(tmp_path: Path) -> None:
    raw = yaml.safe_load(_write_config(tmp_path).read_text(encoding="utf-8"))
    raw["upgrade"] = {
        "transfer": "http",
        "remote_path": "/tmp/firmware.bin",
        "command": "sysupgrade -n /tmp/firmware.bin",
        "boot_timeout_sec": 5,
        "transfer_timeout_sec": 5,
        "http_host": "127.0.0.1",
        "verify_sha256": True,
    }
    raw["dut"] = {
        "name": "dut-int",
        "serial": "/dev/fake",
        "prompt": r"root@OpenWrt:.*# ",
        "connect_timeout_sec": 1,
        "command_timeout_sec": 1,
    }
    raw["tests"] = {
        "smoke": [{"command": "cat /proc/uptime", "expect": r"^UP$"}],
        "command_timeout_sec": 1,
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,
            b"download ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"rebooted\n" + prompt,
            status_json + prompt,
            b"123.45 67.89\n" + prompt,
        ]
    )
    fake_session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )

    workflow = BuildWorkflow(
        config_path,
        docker_client=FakeDockerBuildClient(builder=config.builder),
        dut_workflow_kwargs={
            "serial_session": fake_session,
            "firmware_server": FakeFirmwareServer(port=8888),
        },
    )

    with pytest.raises(WorkflowError, match=r"post-upgrade tests failed.*smoke"):
        workflow.run(dry_run=False, allow_flash=True)

    run_dirs = sorted((tmp_path / "artifacts").glob("job_*"))
    assert len(run_dirs) == 1
    report_md = (run_dirs[0] / "report.md").read_text(encoding="utf-8")
    assert "State: `FAILED`" in report_md
    assert "## Smoke Tests" in report_md
    assert "Result: **FAIL**" in report_md
    assert "`cat /proc/uptime`: failed" in report_md
    rows = JobStore(config.state_db_path(config_path)).recent_metrics(limit=1)
    assert rows[0]["result"] == "failed"
    assert rows[0]["metrics"]["test_duration_sec"] >= rows[0]["metrics"]["smoke_duration_sec"]


def test_build_workflow_does_not_invoke_docker_during_dry_run(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)

    report = workflow.run(dry_run=True, allow_flash=False)

    assert report.success is True
    assert report.state == JobState.DRY_RUN.value
    # Critical safety property: dry-run must not touch external systems.
    assert fake.preflight_calls == 0
    assert fake.run_build_calls == 0
    assert fake.list_artifacts_calls == []
    assert fake.copy_artifact_calls == []


def test_build_workflow_resume_from_artifact_exported_uses_injected_client(
    tmp_path: Path,
) -> None:
    """Resume from ARTIFACT_EXPORTED skips the Docker build entirely; the injected
    fake client must still be the one DutWorkflow's planning sees so the test does
    not accidentally drop into a real DockerBuildClient when the workflow is resumed.
    """
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)

    # First, run a normal build to populate state.
    report = workflow.run(dry_run=False, allow_flash=False)
    job_id = report.job_id
    artifact = report.artifact
    assert artifact is not None

    # Reset the fake's counters so we can assert nothing happens on resume.
    fake.preflight_calls = 0
    fake.run_build_calls = 0
    fake.list_artifacts_calls.clear()
    fake.copy_artifact_calls.clear()

    # Mark the job back to ARTIFACT_EXPORTED so resume() considers it incomplete.
    state_db = workflow.config.state_db_path(config_path.resolve())
    import sqlite3

    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, result = NULL, finished_at = NULL WHERE id = ?",
            (JobState.ARTIFACT_EXPORTED.value, job_id),
        )

    resumed = workflow.resume(job_id, dry_run=True, allow_flash=False)

    assert resumed.success is True
    assert resumed.state == JobState.DRY_RUN.value
    # Build / artifact-export side must be untouched on a dry-run resume from EXPORTED.
    assert fake.preflight_calls == 0
    assert fake.run_build_calls == 0


def test_build_workflow_fails_when_no_artifacts_match(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    # Override list_artifacts to return nothing.
    fake.list_artifacts = lambda patterns: []  # type: ignore[assignment]
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError, match=r"(no artifacts|matched)"):
        workflow.run(dry_run=False, allow_flash=False)

    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(_only_job_id(state_db))
    assert record is not None
    assert record["state"] == JobState.FAILED.value
    # The build itself succeeded; failure is at artifact-select time.
    run_dir = Path(record["artifact_dir"])
    report_md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Classification: `success`" in report_md  # build classified before failure


def test_build_workflow_skips_min_size_filtered_artifacts(tmp_path: Path) -> None:
    """`min_size_mb` mismatch is itself an ArtifactSelectionError that should land
    cleanly in FAILED with the build_summary still attached."""
    config_path = _write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["artifact"]["min_size_mb"] = 999  # nothing the fake produces will pass
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    workflow = BuildWorkflow(config_path, docker_client=fake)

    with pytest.raises(WorkflowError):
        workflow.run(dry_run=False, allow_flash=False)

    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).get_job(_only_job_id(state_db))
    assert record is not None
    assert record["state"] == JobState.FAILED.value


def _only_job_id(state_db: Path) -> str:
    import sqlite3

    with sqlite3.connect(state_db) as conn:
        row = conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()
    assert row is not None, "expected a job to be persisted"
    return row[0]


# Cancellation safety: writing the marker file must short-circuit before docker is touched.
def test_build_workflow_pre_cancellation_skips_docker(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    fake = FakeDockerBuildClient(builder=_builder_from_config(config_path))
    # Pre-place the cancel marker in the run directory the workflow will pick.
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    import owrt_monitor.workflow as workflow_module

    original_new_id = workflow_module._new_job_id
    workflow_module._new_job_id = lambda: "job_pre_cancelled"
    try:
        run_dir = artifact_root / "job_pre_cancelled"
        run_dir.mkdir(parents=True)
        CancelToken(cancel_marker_path(run_dir)).request()

        workflow = BuildWorkflow(config_path, docker_client=fake)
        with pytest.raises(WorkflowError, match="cancel"):
            workflow.run(dry_run=False, allow_flash=False)
    finally:
        workflow_module._new_job_id = original_new_id

    assert fake.preflight_calls == 0
    assert fake.run_build_calls == 0
