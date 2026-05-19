from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from fake_docker import FakeDockerBuildClient
from owrt_monitor.config import load_config
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.state import JobState
from owrt_monitor.workflow import BuildWorkflow


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


def test_full_flow_with_scp_transfer(tmp_path: Path) -> None:
    scp_record = tmp_path / "scp-args.json"
    scp_copy = tmp_path / "scp-copy.bin"
    fake_scp = _make_fake_scp(tmp_path, scp_record=scp_record, scp_copy=scp_copy)
    identity_file = tmp_path / "id_dropbear"
    identity_file.write_text("fake-key", encoding="utf-8")

    config_path = _write_scp_config(
        tmp_path,
        scp_binary=fake_scp,
        identity_file=identity_file,
    )
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"rebooted\n" + prompt,
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
    assert report.artifact is not None
    assert scp_copy.read_bytes() == report.artifact.host_path.read_bytes()

    args = json.loads(scp_record.read_text(encoding="utf-8"))
    assert args[:4] == ["-P", "2222", "-i", str(identity_file)]
    assert "-O" in args
    assert args[-2] == str(report.artifact.host_path)
    assert args[-1] == "root@192.0.2.10:/tmp/firmware.bin"

    written = b"".join(transport.writes)
    assert b"wget -O" not in written
    assert b"tftp -g -r" not in written
    assert b"test $(wc -c < /tmp/firmware.bin)" in written
    assert b"sysupgrade -n /tmp/firmware.bin" in written


def _make_fake_scp(tmp_path: Path, *, scp_record: Path, scp_copy: Path) -> str:
    script = tmp_path / "fake_scp.py"
    script.write_text(
        "#!"
        + os.sys.executable
        + "\n"
        + "import json, shutil, sys\n"
        + "from pathlib import Path\n"
        + f"Path({str(scp_record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        + f"shutil.copyfile(sys.argv[-2], Path({str(scp_copy)!r}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def _write_scp_config(
    tmp_path: Path,
    *,
    scp_binary: str,
    identity_file: Path,
) -> Path:
    raw = {
        "project": {
            "name": "scp-transfer",
            "artifact_dir": str(tmp_path / "artifacts"),
        },
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
            "name": "dut-scp",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
        },
        "upgrade": {
            "transfer": "scp",
            "scp_binary": scp_binary,
            "scp_host": "192.0.2.10",
            "scp_port": 2222,
            "scp_identity_file": str(identity_file),
            "scp_extra_args": ["-O", "-o", "StrictHostKeyChecking=no"],
            "remote_path": "/tmp/firmware.bin",
            "command": "sysupgrade -n /tmp/firmware.bin",
            "boot_timeout_sec": 5,
            "transfer_timeout_sec": 5,
            "verify_sha256": True,
        },
        "tests": {
            "smoke": ["ubus call system board"],
            "command_timeout_sec": 1,
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
