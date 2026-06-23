from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fake_docker import FakeDockerBuildClient, FakeFirmwareServer
from owrt_monitor.artifacts import ExportedArtifact
from owrt_monitor.config import ConfigError, OwrtConfig, load_config
from owrt_monitor.docker_build import sha256_file
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import (
    BuildWorkflow,
    FlashWorkflow,
    WorkflowError,
    _assert_artifact_matches_dut,
)


def _artifact(filename: str, tmp_path: Path) -> ExportedArtifact:
    fw = tmp_path / filename
    fw.write_bytes(b"x")
    return ExportedArtifact(
        container_path="<host>",
        host_path=fw,
        filename=filename,
        size_bytes=1,
        sha256=sha256_file(fw),
    )


def _config(tmp_path: Path, expected: str | None) -> OwrtConfig:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "fake", "workdir": "/work", "command": ["make"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {"name": "dut-x", "serial": "/dev/fake"},
    }
    if expected is not None:
        raw["dut"]["expected_artifact_pattern"] = expected
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(p)


def test_assert_pass_through_when_pattern_unset(tmp_path: Path) -> None:
    config = _config(tmp_path, expected=None)
    artifact = _artifact("anything.bin", tmp_path)
    # No exception expected.
    _assert_artifact_matches_dut(config, artifact)


def test_assert_passes_when_filename_matches(tmp_path: Path) -> None:
    config = _config(tmp_path, expected=r"mediatek_mt7987a-emmc")
    artifact = _artifact(
        "openwrt-mediatek-mt7987-mediatek_mt7987a-emmc-squashfs-sysupgrade.bin",
        tmp_path,
    )
    _assert_artifact_matches_dut(config, artifact)


def test_assert_raises_on_mismatch(tmp_path: Path) -> None:
    config = _config(tmp_path, expected=r"mediatek_mt7987a-emmc")
    # Wrong variant for an AP eMMC profile (sd instead of emmc).
    artifact = _artifact(
        "openwrt-mediatek-mt7987-mediatek_mt7987a-sd-squashfs-sysupgrade.bin",
        tmp_path,
    )
    with pytest.raises(WorkflowError, match=r"does not match expected pattern"):
        _assert_artifact_matches_dut(config, artifact)


def test_invalid_regex_in_config_rejected_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"expected_artifact_pattern is not a valid regex"):
        _config(tmp_path, expected=r"[")


def test_build_workflow_refuses_flash_with_wrong_variant(tmp_path: Path) -> None:
    """End-to-end: even if Docker successfully builds and exports a file, a
    mismatch with `dut.expected_artifact_pattern` aborts before sysupgrade."""
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
            "name": "ap-dut",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
            # FakeDockerBuildClient produces "openwrt-fake-emmc-squashfs-sysupgrade.bin".
            # Configure a pattern that requires "wifi7" — guaranteed to mismatch.
            "expected_artifact_pattern": "wifi7",
        },
        "tests": {"smoke": [], "command_timeout_sec": 1},
        "upgrade": {
            "transfer": "http",
            "http_host": "127.0.0.1",
            "boot_timeout_sec": 1,
            "transfer_timeout_sec": 1,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)
    workflow = BuildWorkflow(
        config_path,
        docker_client=fake_docker,
        dut_workflow_kwargs={
            "serial_session": _stub_session(config, tmp_path),
            "firmware_server": FakeFirmwareServer(),
        },
    )

    with pytest.raises(WorkflowError, match=r"does not match expected pattern.*wifi7"):
        workflow.run(dry_run=False, allow_flash=True)

    # Job lands in FAILED state; build/export still happened, just not the flash.
    state_db = workflow.config.state_db_path(config_path.resolve())
    record = JobStore(state_db).recent_jobs(limit=1)[0]
    assert record["state"] == JobState.FAILED.value


def test_flash_workflow_blocks_wrong_variant_too(tmp_path: Path) -> None:
    artifact_path = tmp_path / "openwrt-wrong-variant.bin"
    artifact_path.write_bytes(b"x")
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "fake", "workdir": "/work", "command": ["make"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {
            "name": "ap-dut",
            "serial": "/dev/fake",
            "expected_artifact_pattern": "must-match-this",
        },
        "upgrade": {"transfer": "http", "http_host": "127.0.0.1"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    workflow = FlashWorkflow(config_path)

    with pytest.raises(WorkflowError, match=r"does not match expected pattern"):
        workflow.run(artifact_path=artifact_path, dry_run=False, allow_flash=True)


def test_flash_workflow_dry_run_blocks_wrong_variant_too(tmp_path: Path) -> None:
    artifact_path = tmp_path / "openwrt-mediatek_mt7987a-spim-nand-sysupgrade.bin"
    artifact_path.write_bytes(b"x")
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "fake", "workdir": "/work", "command": ["make"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {
            "name": "ap-dut",
            "serial": "/dev/fake",
            "expected_artifact_pattern": "ASUS-EAP5000",
        },
        "upgrade": {"transfer": "http", "http_host": "127.0.0.1"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    workflow = FlashWorkflow(config_path)

    with pytest.raises(WorkflowError, match=r"does not match expected pattern.*ASUS-EAP5000"):
        workflow.run(artifact_path=artifact_path, dry_run=True)


def test_resume_dry_run_blocks_wrong_exported_variant(tmp_path: Path) -> None:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "fake", "workdir": "/work", "command": ["make"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": {
            "name": "ap-dut",
            "serial": "/dev/fake",
            "expected_artifact_pattern": "ASUS-EAP5000",
        },
        "upgrade": {"transfer": "http", "http_host": "127.0.0.1"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    workflow = BuildWorkflow(config_path)
    store = JobStore(workflow.config.state_db_path(config_path.resolve()))
    run_dir = workflow.artifact_root / "job_wrong_variant"
    (run_dir / "firmware").mkdir(parents=True)
    firmware = run_dir / "firmware" / "openwrt-mediatek_mt7987a-spim-nand-sysupgrade.bin"
    firmware.write_bytes(b"x")
    store.create_job(
        job_id="job_wrong_variant",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.ARTIFACT_EXPORTED.value,
        config_snapshot=workflow.config.redacted_dump(),
    )
    store.record_artifact(
        job_id="job_wrong_variant",
        container_path="/work/openwrt/bin/openwrt.bin",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=firmware.stat().st_size,
        sha256=sha256_file(firmware),
    )

    with pytest.raises(WorkflowError, match=r"does not match expected pattern.*ASUS-EAP5000"):
        workflow.resume("job_wrong_variant", dry_run=True)


def _stub_session(config, tmp_path: Path) -> SerialSession:
    """Stub session — only used because the workflow constructs DutWorkflow
    even for paths that abort before any serial I/O. Never actually reads."""

    class _Empty:
        in_waiting = 0

        def read(self, size: int = 1) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            pass

    return SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=tmp_path / "serial.log",
        transport=_Empty(),
    )
