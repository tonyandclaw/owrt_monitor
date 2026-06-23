from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from owrt_monitor.retention import (
    _parse_started_at,
    apply_prune,
    format_bytes,
    plan_prune,
)
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def _seed_job(
    store: JobStore,
    artifact_root: Path,
    *,
    job_id: str,
    result: str,
    payload_bytes: int = 1024,
) -> Path:
    run_dir = artifact_root / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text("ok\n", encoding="utf-8")
    (run_dir / "firmware").mkdir(parents=True, exist_ok=True)
    (run_dir / "firmware" / "fake.bin").write_bytes(b"x" * payload_bytes)
    store.create_job(
        job_id=job_id,
        config_path=artifact_root / "config.yaml",
        artifact_dir=run_dir,
        state=JobState.PENDING.value,
        config_snapshot={},
    )
    state = (
        JobState.SUCCEEDED.value if result == "success"
        else JobState.FAILED.value if result == "failed"
        else JobState.CANCELLED.value
    )
    store.update_job(job_id=job_id, state=state, result=result)
    # Tiny delay so started_at differs across rows for ordering tests.
    time.sleep(0.001)
    return run_dir


def test_plan_prune_keeps_newest_per_result(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    store = JobStore(tmp_path / "state.sqlite3")
    # 3 successes, 2 failures, 1 cancelled — in chronological order.
    for i in range(3):
        _seed_job(store, artifact_root, job_id=f"s{i}", result="success")
    for i in range(2):
        _seed_job(store, artifact_root, job_id=f"f{i}", result="failed")
    _seed_job(store, artifact_root, job_id="c0", result="cancelled")

    plan = plan_prune(
        store,
        keep_success=1,
        keep_failed=1,
        keep_other=0,
        artifact_root=artifact_root,
    )

    target_ids = {t.job_id for t in plan.targets}
    # Successes: keep newest s2; prune s0, s1.
    assert "s0" in target_ids
    assert "s1" in target_ids
    assert "s2" not in target_ids
    # Failures: keep newest f1; prune f0.
    assert "f0" in target_ids
    assert "f1" not in target_ids
    # Cancelled: keep_other=0 → prune c0.
    assert "c0" in target_ids


def test_plan_prune_skips_jobs_outside_artifact_root(tmp_path: Path) -> None:
    """Jobs whose run_dirs live outside the configured artifact_root must not
    be eligible — defends against pruning unrelated jobs from a shared DB."""
    artifact_root = tmp_path / "artifacts"
    other_root = tmp_path / "other"
    artifact_root.mkdir()
    other_root.mkdir()
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_job(store, artifact_root, job_id="s_inside", result="success")
    _seed_job(store, other_root, job_id="s_outside", result="success")

    plan = plan_prune(store, keep_success=0, artifact_root=artifact_root)

    target_ids = {t.job_id for t in plan.targets}
    assert "s_inside" in target_ids
    assert "s_outside" not in target_ids


def test_plan_prune_skips_run_dirs_already_gone(tmp_path: Path) -> None:
    """A previously-pruned job's row stays in the DB; the planner should not
    list its (vanished) run_dir as a target."""
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    store = JobStore(tmp_path / "state.sqlite3")
    run_dir = _seed_job(store, artifact_root, job_id="ghost", result="success")
    import shutil

    shutil.rmtree(run_dir)
    plan = plan_prune(store, keep_success=0, artifact_root=artifact_root)
    assert "ghost" not in {t.job_id for t in plan.targets}


def test_apply_prune_deletes_run_dirs_and_reports_bytes(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    store = JobStore(tmp_path / "state.sqlite3")
    payload = 100_000
    _seed_job(store, artifact_root, job_id="old", result="success", payload_bytes=payload)
    _seed_job(store, artifact_root, job_id="new", result="success", payload_bytes=payload)

    plan = plan_prune(store, keep_success=1, artifact_root=artifact_root)
    # plan.total_bytes >= payload because the run_dir holds report.md + firmware/.
    assert plan.total_bytes >= payload

    freed = apply_prune(plan.targets)
    assert freed == plan.total_bytes
    assert not (artifact_root / "old").exists()
    assert (artifact_root / "new").exists()


def test_plan_prune_age_based_deletes_jobs_older_than_cutoff(tmp_path: Path) -> None:
    """Age mode deletes every job older than the cutoff, ignoring keep counts."""
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    store = JobStore(tmp_path / "state.sqlite3")
    for i in range(3):
        _seed_job(store, artifact_root, job_id=f"s{i}", result="success")

    # now = 8 days after the jobs were created → cutoff (now - 7d) is in their
    # future, so all are "older than 7 days" even with a generous keep_success.
    future = datetime.now(UTC) + timedelta(days=8)
    plan = plan_prune(
        store,
        max_age_days=7,
        now=future,
        keep_success=100,
        artifact_root=artifact_root,
    )
    assert {t.job_id for t in plan.targets} == {"s0", "s1", "s2"}


def test_plan_prune_age_based_keeps_recent_jobs(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_job(store, artifact_root, job_id="s0", result="success")

    plan = plan_prune(
        store,
        max_age_days=7,
        now=datetime.now(UTC),
        artifact_root=artifact_root,
    )
    assert plan.targets == []
    assert plan.kept_count_by_result.get("success") == 1


def test_parse_started_at_handles_bad_and_naive_values() -> None:
    assert _parse_started_at(None) is None
    assert _parse_started_at("") is None
    assert _parse_started_at("not-a-date") is None
    aware = _parse_started_at("2026-06-23T02:04:47.286219+00:00")
    assert aware is not None and aware.tzinfo is not None
    # naive timestamps are assumed UTC so comparisons never raise.
    naive = _parse_started_at("2026-06-23T02:04:47")
    assert naive is not None and naive.tzinfo == UTC


def test_apply_prune_no_op_when_no_targets(tmp_path: Path) -> None:
    assert apply_prune([]) == 0


def test_format_bytes_handles_units() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert format_bytes(int(2.5 * 1024 * 1024 * 1024)) == "2.5 GB"
