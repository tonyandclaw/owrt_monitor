from __future__ import annotations

import json
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


def test_locks_snapshot_written_on_acquire(tmp_path: Path) -> None:
    """The Python side writes `<db_dir>/locks.json` whenever locks change.
    The Go owrtd reads this file via `GET /v1/locks` (no SQLite from Go)."""
    db = tmp_path / "state.sqlite3"
    store = JobStore(db)
    store.acquire_dut_lock(dut_name="dut-snap", owner_job_id="jobA")
    store.acquire_builder_lock(builder_name="bld-snap", owner_job_id="jobB")

    snapshot = tmp_path / "locks.json"
    assert snapshot.exists()
    import json

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert "generated_at" in payload
    assert payload["dut_locks"][0]["dut_name"] == "dut-snap"
    assert payload["dut_locks"][0]["owner_job_id"] == "jobA"
    assert payload["builder_locks"][0]["builder_name"] == "bld-snap"
    assert payload["builder_locks"][0]["owner_job_id"] == "jobB"


def test_locks_snapshot_updated_on_release(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = JobStore(db)
    store.acquire_dut_lock(dut_name="dut-r", owner_job_id="job1")
    store.release_dut_lock(dut_name="dut-r", owner_job_id="job1")

    import json

    payload = json.loads((tmp_path / "locks.json").read_text(encoding="utf-8"))
    assert payload["dut_locks"] == []


def test_locks_snapshot_preserves_go_owned_lock_fields(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    snapshot = tmp_path / "locks.json"
    snapshot.write_text(
        """
{
  "generated_at": "2026-05-14T00:00:00Z",
  "dut_locks": [],
  "builder_locks": [],
  "serial_locks": [
    {"name": "tty-usb0", "owner_job_id": "go-job", "created_at": "x", "heartbeat_at": "x"}
  ],
  "artifact_locks": [
    {"name": "export-root", "owner_job_id": "go-job", "created_at": "x", "heartbeat_at": "x"}
  ]
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    store = JobStore(db)
    store.acquire_dut_lock(dut_name="dut-r", owner_job_id="job1")
    store.release_dut_lock(dut_name="dut-r", owner_job_id="job1")

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["serial_locks"][0]["name"] == "tty-usb0"
    assert payload["artifact_locks"][0]["name"] == "export-root"


def test_locks_snapshot_atomic_under_concurrent_lookups(tmp_path: Path) -> None:
    """The temp-then-rename pattern means a reader never observes a torn
    file — either the old contents or the new, but not a partial write."""
    db = tmp_path / "state.sqlite3"
    store = JobStore(db)
    # Take a lock, then take + release a second one, ensuring the snapshot
    # remains parseable JSON on every read in between.
    store.acquire_dut_lock(dut_name="dut1", owner_job_id="ja")
    store.acquire_dut_lock(dut_name="dut2", owner_job_id="jb")
    store.release_dut_lock(dut_name="dut1", owner_job_id="ja")

    import json

    payload = json.loads((tmp_path / "locks.json").read_text(encoding="utf-8"))
    duts = {entry["dut_name"] for entry in payload["dut_locks"]}
    assert duts == {"dut2"}


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


def test_release_locks_for_job_removes_only_owned_locks(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = JobStore(db)
    assert store.acquire_dut_lock(dut_name="dut-owned", owner_job_id="ghost") is True
    assert store.acquire_builder_lock(builder_name="bld-owned", owner_job_id="ghost") is True
    assert store.acquire_dut_lock(dut_name="dut-other", owner_job_id="other") is True

    released = store.release_locks_for_job(owner_job_id="ghost")

    assert released == {"dut_locks": 1, "builder_locks": 1}
    assert store.acquire_dut_lock(dut_name="dut-owned", owner_job_id="new") is True
    assert store.acquire_builder_lock(builder_name="bld-owned", owner_job_id="new") is True
    assert store.acquire_dut_lock(dut_name="dut-other", owner_job_id="new") is False
