from __future__ import annotations

import json
from pathlib import Path

from owrt_monitor.inspect import diff_pairs, inspect_job
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def _seed_job(
    store: JobStore,
    tmp_path: Path,
    *,
    job_id: str,
    report_payload: dict | None = None,
    metrics: dict | None = None,
) -> Path:
    run_dir = tmp_path / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_job(
        job_id=job_id,
        config_path=tmp_path / "config.yaml",
        artifact_dir=run_dir,
        state=JobState.SUCCEEDED.value,
        config_snapshot={"project": {}},
    )
    store.update_job(
        job_id=job_id,
        state=JobState.SUCCEEDED.value,
        result="success",
        metrics=metrics,
    )
    if report_payload is not None:
        (run_dir / "report.json").write_text(
            json.dumps(report_payload), encoding="utf-8"
        )
    return run_dir


def test_inspect_returns_none_for_missing_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    assert inspect_job(store, "nope") is None


def test_inspect_pulls_db_and_report_together(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_job(
        store,
        tmp_path,
        job_id="job_a",
        report_payload={
            "artifact": {"filename": "fw.bin", "size_bytes": 100, "sha256": "abc"},
            "build_summary": {"classification": "success", "duration_sec": 200.0},
            "build_metadata": {
                "profile": "ap",
                "git_commit": "deadbeef",
                "make_target": "ap-target",
            },
            "dut_status": {
                "kernel": "5.15.0",
                "hostname": "OpenWrt",
                "release": {"distribution": "OpenWrt", "version": "22.03"},
            },
            "metrics": {"build_duration_sec": 200, "boot_duration_sec": 30},
            "test_results": [{"command": "x", "passed": True}],
            "warnings": [],
        },
        metrics={"build_duration_sec": 200, "boot_duration_sec": 30},
    )
    insp = inspect_job(store, "job_a")
    assert insp is not None
    assert insp.state == JobState.SUCCEEDED.value
    assert insp.result == "success"
    assert insp.artifact == {"filename": "fw.bin", "size_bytes": 100, "sha256": "abc"}
    assert insp.build_summary["classification"] == "success"
    assert insp.build_metadata["profile"] == "ap"
    assert insp.dut_status["kernel"] == "5.15.0"
    assert insp.metrics["boot_duration_sec"] == 30


def test_inspect_works_when_report_json_missing(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_job(store, tmp_path, job_id="job_no_report")
    insp = inspect_job(store, "job_no_report")
    assert insp is not None
    # DB-side fields populated
    assert insp.state == JobState.SUCCEEDED.value
    # Report-side fields gracefully None
    assert insp.artifact is None
    assert insp.build_summary is None
    assert insp.dut_status is None


def test_inspect_works_when_report_json_corrupt(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = _seed_job(store, tmp_path, job_id="job_bad_json")
    (run_dir / "report.json").write_text("not json {", encoding="utf-8")
    insp = inspect_job(store, "job_bad_json")
    assert insp is not None
    assert insp.artifact is None  # corrupt report → soft-degrade


def test_diff_pairs_marks_differences(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_job(
        store, tmp_path, job_id="job_old",
        report_payload={
            "artifact": {"sha256": "AAA", "size_bytes": 100, "filename": "f.bin"},
            "build_metadata": {"git_commit": "abc1234", "profile": "ap"},
            "metrics": {"boot_duration_sec": 30.0},
            "dut_status": {"release": {"version": "22.03"}},
        },
    )
    _seed_job(
        store, tmp_path, job_id="job_new",
        report_payload={
            "artifact": {"sha256": "BBB", "size_bytes": 105, "filename": "f.bin"},
            "build_metadata": {"git_commit": "def5678", "profile": "ap"},
            "metrics": {"boot_duration_sec": 47.0},
            "dut_status": {"release": {"version": "23.05"}},
        },
    )
    left = inspect_job(store, "job_old")
    right = inspect_job(store, "job_new")
    pairs = dict((label, (lv, rv)) for label, lv, rv in diff_pairs(left, right))
    assert pairs["artifact.sha256"] == ("AAA", "BBB")
    assert pairs["provenance.profile"] == ("ap", "ap")  # same
    assert pairs["provenance.git_commit"] == ("abc1234", "def5678")
    assert pairs["dut.release.version"] == ("22.03", "23.05")
    # boot_duration_sec was a float; format applies
    assert pairs["metrics.boot_duration_sec"] == ("30.00", "47.00")


def test_diff_pairs_handles_missing_fields_gracefully(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_job(store, tmp_path, job_id="job_full",
              report_payload={"artifact": {"sha256": "X"}})
    _seed_job(store, tmp_path, job_id="job_bare")  # no report.json at all
    left = inspect_job(store, "job_full")
    right = inspect_job(store, "job_bare")
    pairs = dict((label, (lv, rv)) for label, lv, rv in diff_pairs(left, right))
    # Missing fields stringify as "—" sentinel.
    assert pairs["artifact.sha256"] == ("X", "—")
    assert pairs["provenance.profile"] == ("—", "—")
