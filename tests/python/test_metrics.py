from __future__ import annotations

from pathlib import Path

from owrt_monitor.metrics import aggregate_metrics
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def test_aggregate_handles_empty_input() -> None:
    summary = aggregate_metrics([])
    assert summary.total_jobs == 0
    assert summary.counts_by_result == {}
    assert summary.success_rate is None
    assert summary.durations == {}


def test_aggregate_counts_results_and_computes_success_rate() -> None:
    rows = [
        {"result": "success", "metrics": {}},
        {"result": "success", "metrics": {}},
        {"result": "failed", "metrics": {}},
        {"result": "cancelled", "metrics": {}},
        {"result": "dry-run", "metrics": {}},
    ]
    summary = aggregate_metrics(rows)
    assert summary.total_jobs == 5
    assert summary.counts_by_result == {
        "success": 2,
        "failed": 1,
        "cancelled": 1,
        "dry-run": 1,
    }
    # 2 success / (2 success + 1 failed) = 0.6667
    assert summary.success_rate == 2 / 3


def test_aggregate_duration_stats_handle_outliers() -> None:
    # Eleven values 1..11 → mean 6, median 6, p90 = 9 + 0.9*(10-9)? Actually with linear
    # interpolation, p90 of 1..11 sorted = rank 0.9*10 = 9 → values[9] = 10.
    rows = [
        {"result": "success", "metrics": {"build_duration_sec": float(v)}}
        for v in range(1, 12)
    ]
    summary = aggregate_metrics(rows)
    stats = summary.durations["build_duration_sec"]
    assert stats.count == 11
    assert stats.mean == 6.0
    assert stats.median == 6.0
    assert stats.p90 == 10.0
    assert stats.minimum == 1.0
    assert stats.maximum == 11.0


def test_aggregate_skips_jobs_missing_metric() -> None:
    rows = [
        {"result": "success", "metrics": {"build_duration_sec": 100.0}},
        {"result": "success", "metrics": {}},  # no boot duration
        {"result": "success", "metrics": {"boot_duration_sec": 30.0}},
    ]
    summary = aggregate_metrics(rows)
    assert summary.durations["build_duration_sec"].count == 1
    assert summary.durations["boot_duration_sec"].count == 1


def test_aggregate_ignores_non_numeric_metrics() -> None:
    rows = [
        {"result": "success", "metrics": {"build_duration_sec": "fast"}},
        {"result": "success", "metrics": {"build_duration_sec": 100.0}},
    ]
    summary = aggregate_metrics(rows)
    assert summary.durations["build_duration_sec"].count == 1
    assert summary.durations["build_duration_sec"].mean == 100.0


def test_recent_metrics_returns_decoded_metrics_dict(tmp_path: Path) -> None:
    """Round-trip: persist via update_job, read back via recent_metrics."""
    store = JobStore(tmp_path / "state.sqlite3")
    for i in range(3):
        store.create_job(
            job_id=f"job_{i}",
            config_path=tmp_path / "config.yaml",
            artifact_dir=tmp_path / f"job_{i}",
            state=JobState.PENDING.value,
            config_snapshot={"project": {}},
        )
        store.update_job(
            job_id=f"job_{i}",
            state=JobState.SUCCEEDED.value,
            result="success",
            metrics={"build_duration_sec": 100.0 + i, "boot_duration_sec": 30.0 + i},
        )
    rows = store.recent_metrics(limit=10)
    assert len(rows) == 3
    for row in rows:
        assert "build_duration_sec" in row["metrics"]
        assert "boot_duration_sec" in row["metrics"]
        assert row["result"] == "success"


def test_recent_metrics_handles_jobs_with_no_metrics(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    store.create_job(
        job_id="job_nometrics",
        config_path=tmp_path / "config.yaml",
        artifact_dir=tmp_path / "job_nometrics",
        state=JobState.PENDING.value,
        config_snapshot={"project": {}},
    )
    store.update_job(
        job_id="job_nometrics",
        state=JobState.FAILED.value,
        result="failed",
    )
    rows = store.recent_metrics(limit=10)
    assert len(rows) == 1
    assert rows[0]["metrics"] == {}
