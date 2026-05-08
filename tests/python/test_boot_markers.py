from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fake_docker import FakeDockerBuildClient
from owrt_monitor.config import ConfigError, load_config
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.dut_workflow import DutWorkflow, DutWorkflowError
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import BuildWorkflow, WorkflowError


class _FakeTransport:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.writes: list[bytes] = []

    @property
    def in_waiting(self) -> int:
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, size: int = 1) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        pass


def test_invalid_boot_marker_regex_rejected_at_load(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "x", "workdir": "/w", "command": ["m"]},
        "artifact": {"patterns": ["*.bin"]},
        "upgrade": {"expected_boot_markers": [r"["]},  # invalid regex
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"expected_boot_markers contains invalid regex"):
        load_config(config_path)


def test_verify_boot_markers_passes_when_all_present(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path, markers=[r"BusyBox v", r"OpenWrt"])
    transcript = (
        "Starting kernel ...\n"
        "OpenWrt 22.03 r19...\n"
        "BusyBox v1.36 (built 2024)\n"
        "root@OpenWrt:/# "
    )
    # Should not raise.
    workflow._verify_expected_boot_markers(transcript)


def test_verify_boot_markers_raises_when_any_missing(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path, markers=[r"BusyBox v", r"OpenWrt"])
    transcript = "Starting kernel ...\nLEDE 17.01\nBusyBox v1.36 (built 2024)\nroot@LEDE:/# "
    with pytest.raises(DutWorkflowError, match=r"expected boot markers were absent.*OpenWrt"):
        workflow._verify_expected_boot_markers(transcript)


def test_verify_boot_markers_no_op_when_unset(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path, markers=[])
    workflow._verify_expected_boot_markers("anything goes here")


def test_full_flow_fails_when_boot_marker_missing(tmp_path: Path) -> None:
    """End-to-end: device reboots and the shell prompt comes back as expected,
    but the boot transcript lacks an expected marker. Workflow must fail."""
    config_path = _write_full_config(
        tmp_path,
        markers=[r"BusyBox v", r"DefinitelyAbsentMarker_xyz"],
    )
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    transport = _FakeTransport(
        [
            prompt,
            b"download ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            # Boot stream contains BusyBox but NOT the synthetic "DefinitelyAbsent..." marker.
            b"Starting kernel ...\nBusyBox v1.36\nOpenWrt 22.03\n" + prompt,
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
        dut_workflow_kwargs={"serial_session": fake_session},
    )

    with pytest.raises(WorkflowError, match=r"DefinitelyAbsentMarker_xyz"):
        workflow.run(dry_run=False, allow_flash=True)

    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).recent_jobs(limit=1)[0]
    assert record["state"] == JobState.FAILED.value


def test_full_flow_succeeds_when_all_markers_present(tmp_path: Path) -> None:
    config_path = _write_full_config(
        tmp_path,
        markers=[r"BusyBox v", r"OpenWrt"],
    )
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15","hostname":"OpenWrt"}\n'
    transport = _FakeTransport(
        [
            prompt,
            b"download ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"Starting kernel ...\nBusyBox v1.36\nOpenWrt 22.03 r19\n" + prompt,
            status_json + prompt,
            b"board ok\n" + prompt,
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
        dut_workflow_kwargs={"serial_session": fake_session},
    )
    report = workflow.run(dry_run=False, allow_flash=True)
    assert report.success is True
    assert report.state == JobState.SUCCEEDED.value


def _make_workflow(tmp_path: Path, *, markers: list[str]) -> DutWorkflow:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "x", "workdir": "/w", "command": ["m"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {"name": "dut-bm", "serial": "/dev/fake"},
        "upgrade": {
            "transfer": "http",
            "http_host": "127.0.0.1",
            "expected_boot_markers": markers,
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(p)
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_job(
        job_id="job_bm",
        config_path=p,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot={},
    )
    logger = EventLogger(store=store, job_id="job_bm", path=run_dir / "events.jsonl")
    return DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_bm",
    )


def _write_full_config(tmp_path: Path, *, markers: list[str]) -> Path:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {
            "container": "fake-builder",
            "workdir": "/work",
            "command": ["make", "fake.profile"],
        },
        "artifact": {
            "patterns": ["build/fake/bin/target/openwrt-*-sysupgrade.bin"],
            "selection": "newest",
            "min_size_mb": 0,
        },
        "dut": {
            "name": "dut-bm",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
        },
        "upgrade": {
            "transfer": "http",
            "http_host": "127.0.0.1",
            "remote_path": "/tmp/firmware.bin",
            "boot_timeout_sec": 1,
            "transfer_timeout_sec": 1,
            "expected_boot_markers": markers,
        },
        "tests": {"smoke": ["ubus call system board"], "command_timeout_sec": 1},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
