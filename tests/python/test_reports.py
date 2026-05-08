from __future__ import annotations

from pathlib import Path

from owrt_monitor.reports import WorkflowReport, write_report


def _empty_report(tmp_path: Path, **fields) -> WorkflowReport:
    return WorkflowReport(
        job_id="job_test",
        state="SUCCEEDED",
        success=True,
        dry_run=False,
        run_dir=tmp_path,
        **fields,
    )


def test_smoke_test_section_aggregates_pass_fail(tmp_path: Path) -> None:
    report = _empty_report(
        tmp_path,
        test_results=[
            {"command": "uptime", "passed": True, "duration_sec": 0.12, "output": "ok"},
            {
                "command": "ubus call system board",
                "passed": True,
                "duration_sec": 0.34,
                "output": "ok",
            },
            {
                "command": "/etc/init.d/network status",
                "passed": False,
                "duration_sec": 0.50,
                "output": "fail",
            },
        ],
    )
    write_report(report)

    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Smoke Tests" in md
    # Aggregate header line: 2/3 passed, total duration 0.96 s
    assert "Result: **FAIL**" in md
    assert "2/3 passed" in md
    assert "1 failed" in md
    assert "0.9 s total" in md or "1.0 s total" in md
    # Per-command rows include duration
    assert "`uptime`: passed (0.12 s)" in md
    assert "`/etc/init.d/network status`: failed (0.50 s)" in md


def test_metrics_section_renders_known_keys_in_order(tmp_path: Path) -> None:
    report = _empty_report(
        tmp_path,
        metrics={
            "build_duration_sec": 302.5,
            "boot_duration_sec": 47.12,
            "smoke_duration_sec": 1.234,
            # An unknown extra key should still render but after the known ones.
            "custom_extra": "informational",
        },
    )
    write_report(report)

    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Metrics" in md
    assert "- build_duration_sec: `302.50 s`" in md
    assert "- boot_duration_sec: `47.12 s`" in md
    assert "- smoke_duration_sec: `1.23 s`" in md
    assert "- custom_extra: `informational`" in md
    # Order: build before boot before smoke before extras.
    assert md.index("build_duration_sec") < md.index("boot_duration_sec")
    assert md.index("boot_duration_sec") < md.index("smoke_duration_sec")
    assert md.index("smoke_duration_sec") < md.index("custom_extra")


def test_metrics_section_omitted_when_no_metrics(tmp_path: Path) -> None:
    report = _empty_report(tmp_path)  # metrics defaults to None
    write_report(report)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Metrics" not in md


def test_smoke_test_section_pass_verdict_when_all_passed(tmp_path: Path) -> None:
    report = _empty_report(
        tmp_path,
        test_results=[
            {"command": "uptime", "passed": True, "duration_sec": 0.1, "output": ""},
        ],
    )
    write_report(report)

    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Result: **PASS**" in md
    assert "1/1 passed" in md
