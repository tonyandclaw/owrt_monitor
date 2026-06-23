from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from fake_docker import FakeDockerBuildClient
from owrt_monitor.artifacts import ExportedArtifact
from owrt_monitor.config import load_config
from owrt_monitor.docker_build import sha256_file
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.dut_workflow import DutWorkflow, DutWorkflowError
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import BuildWorkflow, WorkflowError


class _FakeSerialTransport:
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


def test_publish_to_tftp_root_copies_artifact_with_metadata(tmp_path: Path) -> None:
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id="job_publish",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(store=store, job_id="job_publish", path=run_dir / "events.jsonl")
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_publish",
    )

    firmware = run_dir / "firmware" / "openwrt-fake-sysupgrade.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"FAKE_FIRMWARE_BYTES" * 100)
    artifact = ExportedArtifact(
        container_path="<host>",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=firmware.stat().st_size,
        sha256=sha256_file(firmware),
    )

    destination = workflow._publish_to_tftp_root(artifact)

    assert destination == Path(config.upgrade.tftp_root) / firmware.name
    assert destination.exists()
    assert destination.read_bytes() == firmware.read_bytes()
    # Per-job copy is preserved (audit requirement).
    assert firmware.exists()


def test_publish_to_tftp_root_replaces_unwritable_existing_file(tmp_path: Path) -> None:
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id="job_publish_replace",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(
        store=store, job_id="job_publish_replace", path=run_dir / "events.jsonl"
    )
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_publish_replace",
    )

    firmware = run_dir / "firmware" / "openwrt-fake-sysupgrade.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"NEW_FIRMWARE_BYTES" * 100)
    artifact = ExportedArtifact(
        container_path="<host>",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=firmware.stat().st_size,
        sha256=sha256_file(firmware),
    )

    destination = Path(config.upgrade.tftp_root) / firmware.name
    destination.write_bytes(b"old")
    destination.chmod(0o444)

    published = workflow._publish_to_tftp_root(artifact)

    assert published == destination
    assert destination.read_bytes() == firmware.read_bytes()


def test_publish_to_tftp_root_raises_when_root_missing(tmp_path: Path) -> None:
    config_path = _write_tftp_config(
        tmp_path,
        tftp_root=str(tmp_path / "does_not_exist"),
        create_tftp_root=False,
    )
    config = load_config(config_path)
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id="job_missing_root",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(store=store, job_id="job_missing_root", path=run_dir / "events.jsonl")
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_missing_root",
    )

    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"x")
    artifact = ExportedArtifact(
        container_path="<host>",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=1,
        sha256="0" * 64,
    )
    with pytest.raises(DutWorkflowError, match=r"tftp_root.*does not exist"):
        workflow._publish_to_tftp_root(artifact)


def test_full_flow_with_tftp_transfer(tmp_path: Path) -> None:
    """Run BuildWorkflow.run(allow_flash=True) end-to-end against a TFTP profile.

    Verifies: firmware lands in tftp_root, the DUT sees a `tftp -g -r` command
    pointed at the configured host, and the workflow reaches SUCCEEDED.
    """
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,                       # initial prompt after connect
            b"tftp ok\n" + prompt,        # tftp -g -r ...
            b"size ok\n" + prompt,        # wc -c verification
            b"sha ok\n" + prompt,         # sha256sum verification
            b"rebooted\n" + prompt,       # reboot wait
            status_json + prompt,         # status capture
            b"board ok\n" + prompt,       # smoke test
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
    assert len(report.test_results) == 1

    # Firmware was published to the TFTP root in addition to the per-job copy.
    tftp_root = Path(config.upgrade.tftp_root)
    published = tftp_root / report.artifact.filename
    assert published.exists()
    assert published.stat().st_size == report.artifact.size_bytes
    # Per-job copy is preserved.
    assert report.artifact.host_path.exists()

    # The DUT command stream used `tftp -g`, not `wget`.
    written = b"".join(transport.writes)
    assert b"tftp -g -r" in written
    assert b"-l /tmp/firmware.bin" in written
    assert b"192.0.2.66" in written  # the configured tftp_host (test value)
    assert re.search(rb"tftp -g -r .* 192\.0\.2\.66 \d+\n", written)
    assert b"wget -O" not in written


def test_full_flow_fails_fast_on_kernel_panic(tmp_path: Path) -> None:
    """Inject a panic line into the reboot-wait stream; the workflow must surface
    the panic immediately instead of waiting out boot_timeout_sec."""
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    panic_line = (
        b"[    2.345678] Kernel panic - not syncing: "
        b"Attempted to kill init! exitcode=0x00000004\n"
    )
    transport = _FakeSerialTransport(
        [
            prompt,                      # initial prompt
            b"tftp ok\n" + prompt,       # tftp -g -r
            b"size ok\n" + prompt,       # wc -c
            b"sha ok\n" + prompt,        # sha256sum
            # During reboot wait, panic appears before any prompt could.
            panic_line,
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
    from owrt_monitor.workflow import WorkflowError

    with pytest.raises(WorkflowError, match=r"failed to boot.*Kernel panic"):
        workflow.run(dry_run=False, allow_flash=True)

    state_db = workflow.config.state_db_path(config_path.resolve())
    with __import__("sqlite3").connect(state_db) as conn:
        rows = conn.execute("SELECT id, state, result FROM jobs").fetchall()
    assert len(rows) == 1
    _, state, result = rows[0]
    assert state == JobState.FAILED.value
    assert result == "failed"


def test_tftp_transfer_error_fails_before_sysupgrade(tmp_path: Path) -> None:
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    transport = _FakeSerialTransport(
        [
            prompt,
            b"tftp: sendto: Network unreachable\n" + prompt,
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

    with pytest.raises(WorkflowError, match=r"TFTP firmware download failed.*Network unreachable"):
        workflow.run(dry_run=False, allow_flash=True)

    written = b"".join(transport.writes)
    assert b"tftp -g -r" in written
    assert b"sysupgrade -n" not in written


def test_sysupgrade_image_check_failure_fails_before_smoke_tests(tmp_path: Path) -> None:
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    transport = _FakeSerialTransport(
        [
            prompt,
            b"tftp ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            (
                b"Tue Jun 16 08:55:55 UTC 2026 upgrade: "
                b"Device mediatek,mt7988a-i2p5g-emmc not supported by this image\n"
                b"Tue Jun 16 08:55:55 UTC 2026 upgrade: "
                b"Supported devices: mediatek,mt7988a-ASUS_Controller-emmc\n"
                b"Image check failed.\n"
                + prompt
            ),
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

    with pytest.raises(WorkflowError, match=r"failed during firmware upgrade.*not supported"):
        workflow.run(dry_run=False, allow_flash=True)

    written = b"".join(transport.writes)
    assert b"tftp -g -r" in written
    assert b"sysupgrade -n" in written
    assert b"ubus call system board" not in written

    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).recent_jobs(limit=1)[0]
    assert record["state"] == JobState.FAILED.value


def test_tftp_network_recovery_adds_and_removes_temporary_static_ip(tmp_path: Path) -> None:
    config_path = _write_tftp_config(
        tmp_path,
        network_recovery={
            "enabled": True,
            "ping_host": "192.0.2.66",
            "interface": "br-lan",
            "static_cidr": "192.0.2.1/24",
            "restore_after_transfer": True,
        },
    )
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,
            b"OWRT_PING_RC=1\n" + prompt,
            b"OWRT_PROTO=dhcp\n" + prompt,
            b"added\n" + prompt,
            b"OWRT_PING_RC=0\n" + prompt,
            b"tftp ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"removed\n" + prompt,
            b"booted\n" + prompt,
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
    written = b"".join(transport.writes)
    assert b"ping -c 1 -W 2 192.0.2.66" in written
    assert b"ip addr add 192.0.2.1/24 dev br-lan" in written
    assert b"tftp -g -r" in written
    assert b"ip addr del 192.0.2.1/24 dev br-lan" in written
    assert written.index(b"ip addr add") < written.index(b"tftp -g -r")
    assert written.index(b"tftp -g -r") < written.index(b"ip addr del")
    assert written.index(b"ip addr del") < written.index(b"sysupgrade -n")


def test_tftp_network_recovery_uses_console_state_even_when_uci_static(tmp_path: Path) -> None:
    config_path = _write_tftp_config(
        tmp_path,
        network_recovery={
            "enabled": True,
            "ping_host": "192.0.2.66",
            "interface": "br-lan",
            "static_cidr": "192.0.2.1/24",
            "restore_after_transfer": True,
        },
    )
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,
            b"echo OWRT_PING_RC=$?\nOWRT_PING_RC=1\n" + prompt,
            b"echo OWRT_PROTO=${proto:-unknown}\nOWRT_PROTO=static\n" + prompt,
            b"added\n" + prompt,
            b"echo OWRT_PING_RC=$?\nOWRT_PING_RC=0\n" + prompt,
            b"tftp ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"removed\n" + prompt,
            b"booted\n" + prompt,
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
    written = b"".join(transport.writes)
    assert b"ip addr add 192.0.2.1/24 dev br-lan" in written
    assert written.index(b"ip addr add") < written.index(b"tftp -g -r")

    events = [
        json.loads(line)
        for line in (report.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    recovery = next(event for event in events if event["event"] == "network_recovery_needed")
    assert recovery["fields"]["proto"] == "static"


def test_tftp_host_interface_feeds_transfer_and_network_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_tftp_config(
        tmp_path,
        network_recovery={
            "enabled": True,
            "interface": "br-lan",
            "static_cidr": "192.0.2.1/24",
            "restore_after_transfer": True,
        },
        upgrade_overrides={"host_interface": "USB 10/100/1000 LAN"},
    )
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    monkeypatch.setattr(
        "owrt_monitor.dut_workflow.infer_host_for_interface",
        lambda interface: "192.0.2.88",
    )

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,
            b"OWRT_PING_RC=1\n" + prompt,
            b"OWRT_PROTO=dhcp\n" + prompt,
            b"added\n" + prompt,
            b"OWRT_PING_RC=0\n" + prompt,
            b"tftp ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"removed\n" + prompt,
            b"booted\n" + prompt,
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
    written = b"".join(transport.writes)
    assert b"ping -c 1 -W 2 192.0.2.88" in written
    assert b"tftp -g -r" in written
    assert b"192.0.2.88" in written
    assert b"192.0.2.66" not in written


def test_full_flow_renders_dut_status_section(tmp_path: Path) -> None:
    """The post-boot status capture should produce a `## DUT Status` block in
    the report with the parsed release summary, hostname, and kernel."""
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = (
        b'{"kernel":"5.15.137","hostname":"OpenWrt","board_name":"mt7987",'
        b'"model":"MediaTek MT7987 EVB",'
        b'"release":{"distribution":"OpenWrt","version":"SNAPSHOT"}}\n'
    )
    transport = _FakeSerialTransport(
        [
            prompt,
            b"tftp ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"booted\n" + prompt,
            status_json + prompt,        # status capture
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

    assert report.dut_status is not None
    assert report.dut_status["kernel"] == "5.15.137"
    assert report.dut_status["hostname"] == "OpenWrt"
    assert report.dut_status["board"] == "mt7987"

    md = (report.run_dir / "report.md").read_text(encoding="utf-8")
    assert "## DUT Status" in md
    assert "kernel: `5.15.137`" in md
    assert "release.distribution: `OpenWrt`" in md
    assert "release.version: `SNAPSHOT`" in md


def test_full_flow_records_boot_and_smoke_durations(tmp_path: Path) -> None:
    """End-to-end TFTP flow should populate `report.metrics` with boot + smoke durations
    plus the build duration parsed from the build log."""
    config_path = _write_tftp_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,
            b"tftp ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"booted\n" + prompt,
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

    assert report.metrics is not None
    # Build duration parsed from FakeDockerBuildClient's success_log ("01:23.456" → 83.456 s)
    assert report.metrics["build_duration_sec"] == pytest.approx(83.456, abs=0.01)
    # Boot duration is real wall-clock; can't pin a value, only a sane bound.
    assert 0 <= report.metrics["boot_duration_sec"] < 5
    assert 0 <= report.metrics["test_duration_sec"] < 5
    assert 0 <= report.metrics["smoke_duration_sec"] < 5

    md = (report.run_dir / "report.md").read_text(encoding="utf-8")
    assert "## Metrics" in md
    assert "build_duration_sec" in md
    assert "boot_duration_sec" in md
    assert "test_duration_sec" in md


def test_planned_actions_for_tftp_profile_dry_run(tmp_path: Path) -> None:
    config_path = _write_tftp_config(tmp_path)
    workflow = BuildWorkflow(config_path)
    report = workflow.run(dry_run=True, allow_flash=True)

    md = (report.run_dir / "report.md").read_text(encoding="utf-8")
    assert "Publish firmware: copy `<firmware>` to `" in md
    assert "tftp -g -r" in md
    # Should NOT advertise wget when transfer=tftp.
    assert "wget -O" not in md


def _write_tftp_config(
    tmp_path: Path,
    tftp_root: str | None = None,
    create_tftp_root: bool = True,
    network_recovery: dict | None = None,
    upgrade_overrides: dict | None = None,
) -> Path:
    if tftp_root is None:
        tftp_root = str(tmp_path / "tftpboot")
        Path(tftp_root).mkdir(parents=True, exist_ok=True)
    elif create_tftp_root:
        Path(tftp_root).mkdir(parents=True, exist_ok=True)
    raw = {
        "project": {"name": "tftp-test", "artifact_dir": str(tmp_path / "artifacts")},
        "builder": {
            "container": "fake-builder",
            "workdir": "/work/openwrt",
            "command": ["make", "fake.profile"],
        },
        "artifact": {
            "patterns": ["build/fake/bin/target/openwrt-*-sysupgrade.bin"],
            "selection": "newest",
            "min_size_mb": 0,
        },
        "dut": {
            "name": "dut-tftp",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
        },
        "upgrade": {
            "transfer": "tftp",
            "tftp_root": tftp_root,
            "tftp_host": "192.0.2.66",  # TEST-NET-1, never reachable in CI
            "remote_path": "/tmp/firmware.bin",
            "boot_timeout_sec": 1,
            "transfer_timeout_sec": 1,
        },
        "tests": {"smoke": ["ubus call system board"], "command_timeout_sec": 1},
    }
    if network_recovery is not None:
        raw["upgrade"]["network_recovery"] = network_recovery
    if upgrade_overrides is not None:
        raw["upgrade"].update(upgrade_overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
