import json
import os
from pathlib import Path

import pytest
import yaml
from owrt_monitor.artifacts import ExportedArtifact
from owrt_monitor.config import load_config
from owrt_monitor.docker_build import sha256_file
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.dut_workflow import DutWorkflow, DutWorkflowError
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


class FakeTransport:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.writes: list[bytes] = []

    @property
    def in_waiting(self) -> int:
        if not self.chunks:
            return 0
        return len(self.chunks[0])

    def read(self, size: int = 1) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        pass


def test_dut_workflow_with_fake_serial(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = "job_fake"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id=job_id,
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(store=store, job_id=job_id, path=run_dir / "events.jsonl")
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"fake firmware")
    artifact = ExportedArtifact(
        container_path="<host>",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=firmware.stat().st_size,
        sha256=sha256_file(firmware),
    )
    prompt = b"root@OpenWrt:/# "
    transport = FakeTransport(
        [
            prompt,
            b"download ok\n" + prompt,
            b"size ok\n" + prompt,
            b"sha ok\n" + prompt,
            b"rebooting\n" + prompt,
            b"board ok\n" + prompt,
        ]
    )
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=run_dir / "serial.log",
        transport=transport,
    )
    transitions: list[JobState] = []
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id=job_id,
        serial_session=session,
    )

    results = workflow.execute_upgrade_and_tests(
        artifact,
        transition=lambda state, message, fields: transitions.append(state),
    )

    assert [result.passed for result in results] == [True]
    assert JobState.DUT_LOCKED in transitions
    assert JobState.FIRMWARE_TRANSFERRED in transitions
    assert JobState.DUT_ONLINE in transitions
    assert any(b"sysupgrade -n" in write for write in transport.writes)
    assert any(b"wget -O" in write for write in transport.writes)


def test_dut_lock_releases_when_serial_discovery_fails(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["dut"].pop("serial")
    raw["dut"]["discovery_patterns"] = [str(tmp_path / "missing-*")]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(config_path)
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = "job_discovery_failure"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id=job_id,
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(store=store, job_id=job_id, path=run_dir / "events.jsonl")
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id=job_id,
    )

    with pytest.raises(DutWorkflowError):
        workflow.execute_smoke_tests(transition=lambda state, message, fields: None)

    assert store.acquire_dut_lock(dut_name=config.dut.name, owner_job_id="next_job") is True


def test_execute_smoke_tests_runs_host_side_runners(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    script = _make_executable(
        tmp_path / "script_check.py",
        "import os, sys\n"
        "sys.exit(0 if os.environ.get('OWRT_DUT_NAME') == 'dut-test' else 7)\n",
    )
    pytest_file = tmp_path / "test_host.py"
    pytest_file.write_text(
        "import os\n\n"
        "def test_env():\n"
        "    assert os.environ['OWRT_DUT_ADDRESS'] == '192.0.2.1'\n",
        encoding="utf-8",
    )
    fake_ssh = _make_executable(
        tmp_path / "fake_ssh.py",
        "import os, sys\n"
        "assert os.environ['OWRT_DUT_NAME'] == 'dut-test'\n"
        "sys.stdout.write('OpenWrt ssh ok\\n')\n",
    )
    raw["tests"] = {
        "smoke": [{"command": "ubus call system board", "expect": "board ok"}],
        "scripts": [{"name": "script", "path": str(script), "timeout_sec": 5}],
        "pytest": [{"name": "pytest", "path": str(pytest_file), "args": ["-q"], "timeout_sec": 10}],
        "ssh": [
            {
                "name": "ssh",
                "ssh_binary": str(fake_ssh),
                "command": "cat /etc/openwrt_release",
                "expect": "OpenWrt",
                "timeout_sec": 5,
            }
        ],
        "command_timeout_sec": 1,
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = "job_test_runners"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id=job_id,
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(store=store, job_id=job_id, path=run_dir / "events.jsonl")
    prompt = b"root@OpenWrt:/# "
    transport = FakeTransport([prompt, b"board ok\n" + prompt])
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=run_dir / "serial.log",
        transport=transport,
    )
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id=job_id,
        serial_session=session,
    )

    scripts = []
    pytest_results = []
    ssh_results = []
    metrics: dict[str, float] = {}
    smoke = workflow.execute_smoke_tests(
        transition=lambda state, message, fields: None,
        script_results_out=scripts,
        pytest_results_out=pytest_results,
        ssh_results_out=ssh_results,
        metrics=metrics,
    )

    assert [result.passed for result in smoke] == [True]
    assert [result.passed for result in scripts] == [True]
    assert [result.passed for result in pytest_results] == [True]
    assert [result.passed for result in ssh_results] == [True]
    assert metrics["test_duration_sec"] >= metrics["smoke_duration_sec"]
    assert metrics["script_duration_sec"] >= 0
    assert metrics["pytest_duration_sec"] >= 0
    assert metrics["ssh_duration_sec"] >= 0


def test_host_side_runner_skips_and_missing_ssh_host_emit_events(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["dut"]["network"] = {}
    raw["tests"] = {
        "scripts": [
            {"name": "disabled-script", "path": "/bin/true", "enabled": False},
        ],
        "pytest": [
            {"name": "disabled-pytest", "path": "tests/python", "enabled": False},
        ],
        "ssh": [
            {
                "name": "disabled-ssh",
                "host": "192.0.2.44",
                "command": "true",
                "enabled": False,
            },
            {"name": "missing-host", "command": "true"},
        ],
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = "job_runner_events"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id=job_id,
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(store=store, job_id=job_id, path=run_dir / "events.jsonl")
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id=job_id,
    )

    assert workflow.run_script_tests()[0].skipped is True
    assert workflow.run_pytest_tests()[0].skipped is True
    ssh_results = workflow.run_ssh_tests()
    assert ssh_results[0].skipped is True
    assert ssh_results[1].passed is False
    assert "host" in ssh_results[1].output

    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_name = {event["fields"]["name"]: event for event in events}
    assert by_name["disabled-script"]["fields"]["skipped"] is True
    assert by_name["disabled-pytest"]["fields"]["skipped"] is True
    assert by_name["disabled-ssh"]["fields"]["skipped"] is True
    assert by_name["missing-host"]["fields"]["skipped"] is False
    assert by_name["missing-host"]["level"] == "WARN"


def _make_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{os.sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "artifact_dir": str(tmp_path / "artifacts"),
                },
                "builder": {
                    "container": "builder",
                    "workdir": "/work/openwrt",
                    "command": ["make"],
                },
                "artifact": {
                    "patterns": ["bin/*.bin"],
                },
                "dut": {
                    "name": "dut-test",
                    "serial": "/dev/fake",
                    "prompt": r"root@OpenWrt:.*# ",
                    "connect_timeout_sec": 1,
                    "command_timeout_sec": 1,
                    "network": {"address": "192.0.2.1"},
                },
                "upgrade": {
                    "http_bind": "127.0.0.1",
                    "http_host": "127.0.0.1",
                    "boot_timeout_sec": 1,
                    "transfer_timeout_sec": 1,
                },
                "tests": {
                    "smoke": ["ubus call system board"],
                    "command_timeout_sec": 1,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path
