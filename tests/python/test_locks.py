from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from owrt_monitor.storage import JobStore


def test_acquire_returns_false_for_active_lock(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    assert store.acquire_dut_lock(dut_name="dut-a", owner_job_id="job1") is True
    assert store.acquire_dut_lock(dut_name="dut-a", owner_job_id="job2") is False


def test_acquire_breaks_stale_lock(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = JobStore(db)
    assert store.acquire_dut_lock(dut_name="dut-a", owner_job_id="job1") is True

    # Backdate heartbeat to simulate a crashed prior owner.
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE dut_locks SET heartbeat_at = ? WHERE dut_name = ?",
            (stale, "dut-a"),
        )
        conn.commit()

    # Without timeout, still blocked.
    assert store.acquire_dut_lock(dut_name="dut-a", owner_job_id="job2") is False
    # With a 60s timeout, the stale lock is broken and the new owner takes it.
    assert (
        store.acquire_dut_lock(
            dut_name="dut-a",
            owner_job_id="job3",
            lock_timeout_sec=60,
        )
        is True
    )

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT owner_job_id FROM dut_locks WHERE dut_name = ?",
            ("dut-a",),
        ).fetchone()
    assert row["owner_job_id"] == "job3"


def test_builder_lock_blocks_concurrent_acquire(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    assert store.acquire_builder_lock(builder_name="bld-a", owner_job_id="job1") is True
    assert store.acquire_builder_lock(builder_name="bld-a", owner_job_id="job2") is False
    assert store.builder_lock_owner("bld-a") == "job1"


def test_builder_lock_released_allows_reacquire(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    assert store.acquire_builder_lock(builder_name="bld-a", owner_job_id="job1") is True
    store.release_builder_lock(builder_name="bld-a", owner_job_id="job1")
    assert store.acquire_builder_lock(builder_name="bld-a", owner_job_id="job2") is True
    assert store.builder_lock_owner("bld-a") == "job2"


def test_builder_stale_lock_recovered_with_timeout(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = JobStore(db)
    assert store.acquire_builder_lock(builder_name="bld-a", owner_job_id="ghost") is True
    stale = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE builder_locks SET heartbeat_at = ? WHERE builder_name = ?",
            (stale, "bld-a"),
        )
        conn.commit()
    # Without timeout, still blocked.
    assert store.acquire_builder_lock(builder_name="bld-a", owner_job_id="new") is False
    # With a 1h timeout, the 5h-stale lock is broken.
    assert (
        store.acquire_builder_lock(
            builder_name="bld-a",
            owner_job_id="new",
            lock_timeout_sec=3600,
        )
        is True
    )


def test_builder_lock_owner_returns_none_when_unlocked(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    assert store.builder_lock_owner("bld-x") is None


def test_heartbeat_refreshes_lock(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = JobStore(db)
    assert store.acquire_dut_lock(dut_name="dut-a", owner_job_id="job1") is True

    # Manually backdate heartbeat, then refresh and confirm cleanup no longer breaks it.
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE dut_locks SET heartbeat_at = ? WHERE dut_name = ?",
            (stale, "dut-a"),
        )
        conn.commit()

    store.heartbeat_dut_lock(dut_name="dut-a", owner_job_id="job1")

    assert (
        store.acquire_dut_lock(
            dut_name="dut-a",
            owner_job_id="other",
            lock_timeout_sec=60,
        )
        is False
    )
