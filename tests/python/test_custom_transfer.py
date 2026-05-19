from __future__ import annotations

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


def test_full_flow_with_custom_transfer_command(tmp_path: Path) -> None:
    config_path = _write_custom_config(tmp_path)
    config = load_config(config_path)
    fake_docker = FakeDockerBuildClient(builder=config.builder)

    prompt = b"root@OpenWrt:/# "
    status_json = b'{"kernel":"5.15.0","hostname":"OpenWrt"}\n'
    transport = _FakeSerialTransport(
        [
            prompt,                      # initial prompt after connect
            b"size ok\n" + prompt,       # wc -c verification
            b"sha ok\n" + prompt,        # sha256sum verification
            b"rebooted\n" + prompt,      # reboot wait
            status_json + prompt,        # status capture
            b"board ok\n" + prompt,      # smoke test
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
    assert len(report.pytest_results) == 1
    assert report.pytest_results[0]["passed"] is True
    assert len(report.ssh_results) == 1
    assert report.ssh_results[0]["passed"] is True
    copied = tmp_path / "custom-transfer-copy.bin"
    assert copied.read_bytes() == report.artifact.host_path.read_bytes()

    written = b"".join(transport.writes)
    assert b"wget -O" not in written
    assert b"tftp -g -r" not in written
    assert b"test $(wc -c < /tmp/firmware.bin)" in written
    assert b"sha256sum /tmp/firmware.bin" in written
    assert b"sysupgrade -n /tmp/firmware.bin" in written


def _write_custom_config(tmp_path: Path) -> Path:
    pytest_file = tmp_path / "host_tests" / "test_env.py"
    pytest_file.parent.mkdir()
    pytest_file.write_text(
        "import os\n\n"
        "def test_owrt_env_available():\n"
        "    assert os.environ['OWRT_DUT_NAME'] == 'dut-custom'\n"
        "    assert os.environ['OWRT_FIRMWARE_FILENAME'].endswith('.bin')\n",
        encoding="utf-8",
    )
    fake_ssh = tmp_path / "fake_ssh.py"
    fake_ssh.write_text(
        "#!"
        + os.sys.executable
        + "\n"
        + "import os, sys\n"
        + "if not os.environ.get('OWRT_FIRMWARE_FILENAME', '').endswith('.bin'):\n"
        + "    sys.exit(9)\n"
        + "sys.stdout.write('OpenWrt ssh ok\\n')\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    raw = {
        "project": {
            "name": "custom-transfer",
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
            "name": "dut-custom",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
        },
        "upgrade": {
            "transfer": "custom",
            "custom_transfer_command": [
                "/bin/cp",
                "{artifact}",
                str(tmp_path / "custom-transfer-copy.bin"),
            ],
            "remote_path": "/tmp/firmware.bin",
            "command": "sysupgrade -n /tmp/firmware.bin",
            "boot_timeout_sec": 5,
            "transfer_timeout_sec": 5,
            "verify_sha256": True,
        },
        "tests": {
            "smoke": ["ubus call system board"],
            "pytest": [
                {
                    "name": "host-pytest",
                    "path": str(pytest_file),
                    "args": ["-q"],
                    "timeout_sec": 10,
                }
            ],
            "ssh": [
                {
                    "name": "ssh-smoke",
                    "ssh_binary": str(fake_ssh),
                    "host": "192.0.2.20",
                    "command": "cat /etc/openwrt_release",
                    "expect": "OpenWrt",
                    "timeout_sec": 5,
                }
            ],
            "command_timeout_sec": 1,
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
