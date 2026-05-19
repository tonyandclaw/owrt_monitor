from __future__ import annotations

import json
from types import SimpleNamespace

from owrt_monitor.cli import (
    _compact_process_output,
    _daemon_job_payload,
    _inspection_result_line,
    _mark_orphaned_job,
    _network_readiness,
    _ping_command,
    _post_upgrade_summary_lines,
    _serial_prompt_readiness,
    _serial_readiness,
    _submit_daemon_job,
    _transfer_readiness,
)
from owrt_monitor.reports import WorkflowReport, write_report
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def test_post_upgrade_summary_includes_all_runner_types(tmp_path) -> None:
    report = WorkflowReport(
        job_id="job_cli",
        state="SUCCEEDED",
        success=True,
        dry_run=False,
        run_dir=tmp_path,
        test_results=[{"passed": True}, {"passed": False, "skipped": True}],
        script_results=[{"passed": True}],
        pytest_results=[{"passed": True}, {"passed": True}],
        ssh_results=[{"passed": False}],
    )

    assert _post_upgrade_summary_lines(report) == [
        "Smoke tests: [bold]1/2 passed, 1 skipped[/bold]",
        "Custom scripts: [bold]1/1 passed[/bold]",
        "Pytest tests: [bold]2/2 passed[/bold]",
        "SSH tests: [bold]0/1 passed[/bold]",
    ]


def test_inspection_result_line_includes_skipped_count() -> None:
    assert (
        _inspection_result_line(
            "Pytest tests",
            [{"passed": True}, {"passed": False, "skipped": True}],
        )
        == "Pytest tests: [bold]1/2 passed, 1 skipped[/bold]"
    )


def test_mark_orphaned_job_updates_report_and_releases_locks(tmp_path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store.create_job(
        job_id="job_orphan",
        config_path=tmp_path / "config.yaml",
        artifact_dir=run_dir,
        state=JobState.BUILD_RUNNING.value,
        config_snapshot={},
        pid=999999,
    )
    store.acquire_dut_lock(dut_name="dut-orphan", owner_job_id="job_orphan")
    store.acquire_builder_lock(builder_name="builder-orphan", owner_job_id="job_orphan")
    write_report(
        WorkflowReport(
            job_id="job_orphan",
            state=JobState.BUILD_RUNNING.value,
            success=False,
            dry_run=False,
            run_dir=run_dir,
        )
    )

    record = store.get_job("job_orphan")
    assert record is not None
    _mark_orphaned_job(store, record)

    updated = store.get_job("job_orphan")
    assert updated is not None
    assert updated["state"] == JobState.FAILED.value
    assert updated["result"] == "orphan"
    assert store.acquire_dut_lock(dut_name="dut-orphan", owner_job_id="next") is True
    assert store.acquire_builder_lock(builder_name="builder-orphan", owner_job_id="next") is True

    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["state"] == JobState.FAILED.value
    assert report["success"] is False
    assert "marked orphaned" in report["warnings"][0]
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event"] == "job_orphaned"
    assert events[-1]["fields"]["dut_locks"] == 1
    assert events[-1]["fields"]["builder_locks"] == 1


def test_daemon_job_payload_resolves_paths(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    artifact = tmp_path / "firmware.bin"
    config.write_text("project: {}\n", encoding="utf-8")
    artifact.write_bytes(b"fw")

    payload = _daemon_job_payload(
        "flash",
        config=config,
        profile="ap",
        dry_run=True,
        allow_flash=False,
        artifact=artifact,
    )

    assert payload["command"] == "flash"
    assert payload["config"] == str(config.resolve())
    assert payload["profile"] == "ap"
    assert payload["dry_run"] is True
    assert payload["allow_flash"] is False
    assert payload["artifact"] == str(artifact.resolve())
    assert payload["working_dir"]


def test_serial_readiness_reports_missing_configured_port(monkeypatch) -> None:
    monkeypatch.setattr(
        "owrt_monitor.cli.discover_serial_ports",
        lambda patterns: ["/dev/cu.usbserial-8330"],
    )

    ok, detail = _serial_readiness("/dev/cu.usbserial-missing", ["unused"])

    assert ok is False
    assert "configured serial" in detail
    assert "/dev/cu.usbserial-8330" in detail


def test_serial_readiness_requires_explicit_choice_for_multiple_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        "owrt_monitor.cli.discover_serial_ports",
        lambda patterns: ["/dev/cu.usbserial-8330", "/dev/cu.usbserial-8340"],
    )

    ok, detail = _serial_readiness(None, ["unused"])

    assert ok is False
    assert "multiple serial candidates" in detail


def test_serial_prompt_readiness_reports_probe_result(monkeypatch, tmp_path) -> None:
    def fake_probe(config, transcript_path):
        assert config == "config"
        assert transcript_path == tmp_path / "serial.log"
        return "/dev/cu.usbserial-8330"

    monkeypatch.setattr("owrt_monitor.cli.probe_serial_interactive", fake_probe)

    ok, detail = _serial_prompt_readiness(
        "/dev/cu.usbserial-8330",
        config="config",
        transcript_path=tmp_path / "serial.log",
    )

    assert ok is True
    assert "/dev/cu.usbserial-8330 prompt matched" in detail
    assert "serial.log" in detail


def test_transfer_readiness_requires_writable_tftp_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("owrt_monitor.cli.os.access", lambda path, mode: False)

    ok, detail = _transfer_readiness(
        SimpleNamespace(
            transfer="tftp",
            tftp_host="192.168.1.66",
            http_host=None,
            tftp_root=tmp_path,
        )
    )

    assert ok is False
    assert "not writable" in detail


def test_network_readiness_pings_configured_dut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, check, capture_output, text, timeout):
        captured["command"] = command
        captured["timeout"] = timeout
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("owrt_monitor.cli.subprocess.run", fake_run)

    ok, detail = _network_readiness(
        SimpleNamespace(address="192.168.1.1"),
        SimpleNamespace(transfer="tftp"),
    )

    assert ok is True
    assert "reachable" in detail
    assert captured["command"][-1] == "192.168.1.1"
    assert captured["timeout"] == 3


def test_network_readiness_reports_unreachable_dut(monkeypatch) -> None:
    def fake_run(command, check, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="100.0% packet loss", stderr="")

    monkeypatch.setattr("owrt_monitor.cli.subprocess.run", fake_run)

    ok, detail = _network_readiness(
        SimpleNamespace(address="192.168.1.1"),
        SimpleNamespace(transfer="tftp"),
    )

    assert ok is False
    assert "not reachable" in detail
    assert "packet loss" in detail


def test_ping_command_uses_millisecond_timeout_on_macos(monkeypatch) -> None:
    monkeypatch.setattr("owrt_monitor.cli.sys.platform", "darwin")

    assert _ping_command("192.168.1.1") == ["ping", "-c", "1", "-W", "1000", "192.168.1.1"]


def test_compact_process_output_prefers_packet_loss_line() -> None:
    assert (
        _compact_process_output(
            "PING 192.168.1.1\n"
            "--- 192.168.1.1 ping statistics ---\n"
            "1 packets transmitted, 0 packets received, 100.0% packet loss\n"
        )
        == "1 packets transmitted, 0 packets received, 100.0% packet loss"
    )


def test_submit_daemon_job_posts_to_owrtd(monkeypatch, tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("project: {}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "job_id": "job_abc",
                    "pid": 123,
                    "run_dir": str(tmp_path / "job_abc"),
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("owrt_monitor.cli.urllib.request.urlopen", fake_urlopen)

    _submit_daemon_job(
        "build",
        config=config,
        profile=None,
        dry_run=False,
        allow_flash=False,
        artifact=None,
        daemon_url="http://127.0.0.1:8765/",
    )

    assert captured["url"] == "http://127.0.0.1:8765/v1/jobs"
    assert captured["timeout"] == 10
    assert captured["payload"] == {
        "command": "build",
        "config": str(config.resolve()),
        "dry_run": False,
        "allow_flash": False,
        "working_dir": captured["payload"]["working_dir"],
    }
