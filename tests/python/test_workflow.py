from pathlib import Path

import yaml
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import BuildWorkflow, FlashWorkflow, SmokeTestWorkflow


def test_dry_run_writes_job_outputs(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    artifact_dir = tmp_path / "artifacts"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "test-lab",
                    "artifact_dir": str(artifact_dir),
                },
                "builder": {
                    "container": "test-builder",
                    "workdir": "/work/openwrt",
                    "command": ["make", "-j2"],
                    "env": {"API_TOKEN": "super-secret"},
                },
                "artifact": {
                    "patterns": ["bin/targets/**/**/openwrt-*-sysupgrade.bin"],
                    "selection": "newest",
                    "min_size_mb": 1,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = BuildWorkflow(config_path).run(dry_run=True)

    assert report.success is True
    assert (report.run_dir / "config.snapshot.yaml").exists()
    assert (report.run_dir / "events.jsonl").exists()
    assert (report.run_dir / "report.json").exists()
    assert (report.run_dir / "report.md").exists()
    assert "super-secret" not in (report.run_dir / "report.json").read_text(encoding="utf-8")
    assert "super-secret" not in (report.run_dir / "report.md").read_text(encoding="utf-8")

    store = JobStore(artifact_dir / "owrt_monitor.sqlite3")
    rows = store.recent_jobs(limit=1)
    assert rows[0]["id"] == report.job_id
    assert rows[0]["state"] == "DRY_RUN"


def test_flash_dry_run_writes_planned_dut_actions(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    artifact_dir = tmp_path / "artifacts"
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"fake firmware")
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "test-lab",
                    "artifact_dir": str(artifact_dir),
                },
                "builder": {
                    "container": "test-builder",
                    "workdir": "/work/openwrt",
                    "command": ["make"],
                },
                "artifact": {
                    "patterns": ["bin/*.bin"],
                },
                "dut": {
                    "serial": "/dev/fake",
                },
                "upgrade": {
                    "http_host": "192.0.2.10",
                },
                "tests": {
                    "smoke": ["ubus call system board"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = FlashWorkflow(config_path).run(artifact_path=firmware, dry_run=True)

    assert report.success is True
    assert report.artifact is not None
    assert "Firmware transfer" in (report.run_dir / "report.md").read_text(encoding="utf-8")
    assert "Smoke test" in (report.run_dir / "report.md").read_text(encoding="utf-8")


def test_smoke_test_dry_run_writes_planned_actions(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    artifact_dir = tmp_path / "artifacts"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "test-lab",
                    "artifact_dir": str(artifact_dir),
                },
                "builder": {
                    "container": "test-builder",
                    "workdir": "/work/openwrt",
                    "command": ["make"],
                },
                "artifact": {
                    "patterns": ["bin/*.bin"],
                },
                "dut": {
                    "serial": "/dev/fake",
                },
                "tests": {
                    "smoke": ["ubus call system board"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = SmokeTestWorkflow(config_path).run(dry_run=True)

    assert report.success is True
    report_md = (report.run_dir / "report.md").read_text(encoding="utf-8")
    assert "DUT lock" in report_md
    assert "Smoke test" in report_md
