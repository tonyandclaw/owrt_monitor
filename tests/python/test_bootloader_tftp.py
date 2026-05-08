from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fake_docker import FakeDockerBuildClient
from owrt_monitor.config import ConfigError, load_config
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.state import JobState
from owrt_monitor.workflow import BuildWorkflow


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


def test_bootloader_invalid_regex_rejected_at_load(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "x", "workdir": "/w", "command": ["m"]},
        "artifact": {"patterns": ["*.bin"]},
        "upgrade": {
            "transfer": "bootloader_tftp",
            "bootloader": {"prompt": r"["},  # invalid regex
        },
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_bootloader_tftp_full_flow(tmp_path: Path) -> None:
    """End-to-end bootloader-tftp recovery flow.

    Simulated boot stream:
      1. Initial OpenWrt shell prompt (DUT_READY).
      2. After `reboot`: autoboot banner.
      3. After interrupt key: bootloader prompt `=> `.
      4. After each setenv/tftpboot: bootloader prompt back.
      5. After `bootm`: full boot stream + new shell prompt.
      6. Status capture + smoke test responses.
    """
    config_path = _write_bootloader_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    shell_prompt = b"root@OpenWrt:/# "
    bl_prompt = b"=> "
    autoboot = b"Hit any key to stop autoboot\n"
    second_boot = (
        b"\nStarting kernel ...\n"
        b"BusyBox v1.36 (built 2024)\n"
        b"OpenWrt 22.03 r19...\n"
    )
    status_json = b'{"kernel":"5.15","hostname":"OpenWrt"}\n'
    transport = _FakeTransport(
        [
            shell_prompt,                       # 1. initial DUT_READY
            b"system halted\n" + autoboot,      # 2. after `reboot`, autoboot banner
            bl_prompt,                          # 3. after interrupt key, bootloader prompt
            bl_prompt,                          # 4. after `setenv serverip ...`
            bl_prompt,                          # 5. after `setenv ipaddr ...`
            b"Bytes transferred = 29MB\n" + bl_prompt,  # 6. after `tftpboot ...`
            second_boot + shell_prompt,         # 7. after `bootm`, new image boots
            status_json + shell_prompt,         # 8. status capture
            b"board ok\n" + shell_prompt,       # 9. smoke test
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

    # Critical writes happened in the right order.
    written = b"".join(transport.writes)
    assert b"reboot\n" in written
    assert b"setenv serverip 192.168.1.66\n" in written
    assert b"setenv ipaddr 192.168.1.1\n" in written
    assert b"tftpboot 0x80000000 " in written
    assert b"bootm\n" in written

    # Interrupt key was sent (single space, no newline). It appears between
    # the `reboot\n` write and the first `setenv`. We can't pin the exact
    # position because of test simulation, but it must be a one-byte write.
    assert any(w == b" " for w in transport.writes), (
        f"expected a single-byte interrupt-key write; got {transport.writes!r}"
    )

    # Firmware was published to tftp_root.
    tftp_root = Path(config.upgrade.tftp_root)
    published = tftp_root / report.artifact.filename
    assert published.exists()


def test_bootloader_tftp_fails_on_kernel_panic_during_boot(tmp_path: Path) -> None:
    """If a kernel panic appears before the bootloader prompt during the
    autoboot wait, the workflow must surface it as BootFailureError, not
    hang waiting for the autoboot banner."""
    config_path = _write_bootloader_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    shell_prompt = b"root@OpenWrt:/# "
    panic = b"\n[    1.234] Kernel panic - not syncing: bad bootloader\n"
    transport = _FakeTransport(
        [
            shell_prompt,        # initial DUT_READY
            panic,               # after `reboot`, kernel panic during boot
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

    with pytest.raises(WorkflowError, match=r"Kernel panic"):
        workflow.run(dry_run=False, allow_flash=True)


def test_bootloader_tftp_dry_run_renders_plan(tmp_path: Path) -> None:
    config_path = _write_bootloader_config(tmp_path)
    workflow = BuildWorkflow(config_path)
    report = workflow.run(dry_run=True, allow_flash=True)

    md = (report.run_dir / "report.md").read_text(encoding="utf-8")
    # The dry-run plan should show the new bootloader-tftp shape, not wget.
    assert "Reboot into bootloader: send `reboot`" in md
    assert "Bootloader sequence:" in md
    assert "tftpboot 0x80000000" in md
    assert "bootm" in md
    assert "wget" not in md
    # The legacy `Upgrade command:` line is suppressed for bootloader_tftp.
    assert "sysupgrade -n" not in md


def _write_bootloader_config(tmp_path: Path) -> Path:
    tftp_root = tmp_path / "tftpboot"
    tftp_root.mkdir(parents=True, exist_ok=True)
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
            "name": "dut-bl",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
            "network": {"address": "192.168.1.1"},
        },
        "upgrade": {
            "transfer": "bootloader_tftp",
            "tftp_root": str(tftp_root),
            "tftp_host": "192.168.1.66",
            "boot_timeout_sec": 5,
            "transfer_timeout_sec": 5,
            "bootloader": {
                "autoboot_wait_sec": 5,
                "bootloader_prompt_wait_sec": 2,
                "tftp_load_wait_sec": 5,
            },
        },
        "tests": {"smoke": ["ubus call system board"], "command_timeout_sec": 1},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
