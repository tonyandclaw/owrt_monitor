from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from owrt_monitor.artifacts import ExportedArtifact
from owrt_monitor.config import load_config
from owrt_monitor.docker_build import sha256_file
from owrt_monitor.dut_serial import SerialSession
from owrt_monitor.dut_workflow import (
    DutWorkflow,
    DutWorkflowError,
    _parse_busybox_df_avail_kb,
)
from owrt_monitor.events import EventLogger
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def test_parse_busybox_df_one_line() -> None:
    out = (
        "Filesystem           1K-blocks      Used Available Use% Mounted on\n"
        "tmpfs                    65536       128    65408   0% /tmp\n"
    )
    assert _parse_busybox_df_avail_kb(out) == 65408


def test_parse_busybox_df_wrapped_filesystem() -> None:
    # BusyBox wraps a long filesystem path onto its own line.
    out = (
        "Filesystem           1K-blocks      Used Available Use% Mounted on\n"
        "/dev/very/long/mtdblock0/whatever\n"
        "                       128000     32000    96000  25% /tmp\n"
    )
    assert _parse_busybox_df_avail_kb(out) == 96000


def test_parse_busybox_df_handles_garbage() -> None:
    assert _parse_busybox_df_avail_kb("") is None
    assert _parse_busybox_df_avail_kb("not a df output\n") is None


class _FakeTransport:
    """Returns canned chunks for each successive read; writes go to /dev/null."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
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


def test_dut_check_blocks_when_free_space_below_threshold(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        upgrade_overrides={"min_dut_free_kb": 60_000},
    )
    config = load_config(config_path)

    prompt = b"root@OpenWrt:/# "
    df_response = (
        b"Filesystem           1K-blocks      Used Available Use% Mounted on\n"
        b"tmpfs                    65536    60000     5536  91% /tmp\n"
    )
    transport = _FakeTransport([prompt, df_response + prompt])

    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=config.dut.prompt,
        transcript_path=tmp_path / "serial.log",
        transport=transport,
    )

    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id="job_freespace",
        config_path=config_path,
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot=config.redacted_dump(),
    )
    logger = EventLogger(store=store, job_id="job_freespace", path=run_dir / "events.jsonl")
    workflow = DutWorkflow(
        config=config,
        run_dir=run_dir,
        logger=logger,
        store=store,
        job_id="job_freespace",
        serial_session=session,
    )

    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"x" * 4096)
    artifact = ExportedArtifact(
        container_path="<host>",
        host_path=firmware,
        filename=firmware.name,
        size_bytes=firmware.stat().st_size,
        sha256=sha256_file(firmware),
    )

    session.connect()
    session.send_newline()
    session.read_until_prompt(timeout_sec=1)
    with pytest.raises(DutWorkflowError, match=r"5536 KB free"):
        workflow._check_dut_free_space(session, artifact)


def _write_config(tmp_path: Path, upgrade_overrides: dict | None = None) -> Path:
    upgrade = {
        "http_host": "127.0.0.1",
        "boot_timeout_sec": 1,
        "transfer_timeout_sec": 1,
    }
    if upgrade_overrides:
        upgrade.update(upgrade_overrides)
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {
            "container": "builder",
            "workdir": "/work",
            "command": ["make"],
        },
        "artifact": {"patterns": ["bin/*.bin"]},
        "dut": {
            "name": "dut-test",
            "serial": "/dev/fake",
            "prompt": r"root@OpenWrt:.*# ",
            "connect_timeout_sec": 1,
            "command_timeout_sec": 1,
        },
        "upgrade": upgrade,
        "tests": {"command_timeout_sec": 1},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
