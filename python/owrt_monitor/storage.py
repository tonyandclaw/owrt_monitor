from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def create_job(
        self,
        *,
        job_id: str,
        config_path: Path,
        artifact_dir: Path,
        state: str,
        config_snapshot: dict[str, Any],
        pid: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                  id, config_path, artifact_dir, state, result,
                  started_at, finished_at, config_snapshot, pid
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    str(config_path),
                    str(artifact_dir),
                    state,
                    _now(),
                    json.dumps(config_snapshot, sort_keys=True),
                    pid,
                ),
            )

    def update_job(
        self,
        *,
        job_id: str,
        state: str,
        result: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        finished_at = _now() if result is not None else None
        metrics_json = json.dumps(metrics, sort_keys=True) if metrics else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                   SET state = ?,
                       result = COALESCE(?, result),
                       finished_at = COALESCE(?, finished_at),
                       metrics = COALESCE(?, metrics)
                 WHERE id = ?
                """,
                (state, result, finished_at, metrics_json, job_id),
            )

    def record_event(
        self,
        *,
        job_id: str,
        level: str,
        component: str,
        event: str,
        message: str,
        fields: dict[str, Any],
        ts: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO job_events (job_id, ts, level, component, event, message, fields)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    ts or _now(),
                    level,
                    component,
                    event,
                    message,
                    json.dumps(fields, sort_keys=True),
                ),
            )

    def record_artifact(
        self,
        *,
        job_id: str,
        container_path: str,
        host_path: Path,
        filename: str,
        size_bytes: int,
        sha256: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                  job_id, container_path, host_path, filename, size_bytes, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, container_path, str(host_path), filename, size_bytes, sha256, _now()),
            )

    def acquire_builder_lock(
        self,
        *,
        builder_name: str,
        owner_job_id: str,
        lock_timeout_sec: int | None = None,
    ) -> bool:
        acquired = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT owner_job_id, heartbeat_at FROM builder_locks WHERE builder_name = ?",
                (builder_name,),
            ).fetchone()
            if existing is not None:
                if lock_timeout_sec is None or not _is_stale(
                    existing["heartbeat_at"], lock_timeout_sec
                ):
                    connection.rollback()
                    return False
                connection.execute(
                    "DELETE FROM builder_locks WHERE builder_name = ? AND owner_job_id = ?",
                    (builder_name, existing["owner_job_id"]),
                )
            connection.execute(
                """
                INSERT INTO builder_locks (builder_name, owner_job_id, created_at, heartbeat_at)
                VALUES (?, ?, ?, ?)
                """,
                (builder_name, owner_job_id, _now(), _now()),
            )
            connection.commit()
            acquired = True
        if acquired:
            self._refresh_locks_snapshot()
        return True

    def release_builder_lock(self, *, builder_name: str, owner_job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM builder_locks
                 WHERE builder_name = ? AND owner_job_id = ?
                """,
                (builder_name, owner_job_id),
            )
        self._refresh_locks_snapshot()

    def builder_lock_owner(self, builder_name: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_job_id FROM builder_locks WHERE builder_name = ?",
                (builder_name,),
            ).fetchone()
        return row["owner_job_id"] if row is not None else None

    def acquire_dut_lock(
        self,
        *,
        dut_name: str,
        owner_job_id: str,
        lock_timeout_sec: int | None = None,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT owner_job_id, heartbeat_at FROM dut_locks WHERE dut_name = ?",
                (dut_name,),
            ).fetchone()
            if existing is not None:
                if lock_timeout_sec is None or not _is_stale(
                    existing["heartbeat_at"], lock_timeout_sec
                ):
                    connection.rollback()
                    return False
                connection.execute(
                    "DELETE FROM dut_locks WHERE dut_name = ? AND owner_job_id = ?",
                    (dut_name, existing["owner_job_id"]),
                )
            connection.execute(
                """
                INSERT INTO dut_locks (dut_name, owner_job_id, created_at, heartbeat_at)
                VALUES (?, ?, ?, ?)
                """,
                (dut_name, owner_job_id, _now(), _now()),
            )
            connection.commit()
        self._refresh_locks_snapshot()
        return True

    def heartbeat_dut_lock(self, *, dut_name: str, owner_job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dut_locks
                   SET heartbeat_at = ?
                 WHERE dut_name = ? AND owner_job_id = ?
                """,
                (_now(), dut_name, owner_job_id),
            )
        self._refresh_locks_snapshot()

    def release_dut_lock(self, *, dut_name: str, owner_job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM dut_locks
                 WHERE dut_name = ? AND owner_job_id = ?
                """,
                (dut_name, owner_job_id),
            )
        self._refresh_locks_snapshot()

    def release_locks_for_job(self, *, owner_job_id: str) -> dict[str, int]:
        """Release all DUT and builder locks owned by a job.

        Used by orphan recovery after the recorded PID is known dead. Normal
        workflows should still release their precise locks in `finally`.
        """
        with self._connect() as connection:
            dut_cursor = connection.execute(
                "DELETE FROM dut_locks WHERE owner_job_id = ?",
                (owner_job_id,),
            )
            builder_cursor = connection.execute(
                "DELETE FROM builder_locks WHERE owner_job_id = ?",
                (owner_job_id,),
            )
            released = {
                "dut_locks": max(dut_cursor.rowcount, 0),
                "builder_locks": max(builder_cursor.rowcount, 0),
            }
        if released["dut_locks"] or released["builder_locks"]:
            self._refresh_locks_snapshot()
        return released

    def _refresh_locks_snapshot(self) -> None:
        """Atomically write `<db_dir>/locks.json` with the current lock state.

        Companion file for the Go owrtd `GET /v1/locks` endpoint — keeps the
        Go side dep-free (no SQLite driver) at the cost of one tiny file
        rewrite per lock mutation. Atomic via os.replace so the daemon
        never sees a torn read.
        """
        try:
            with self._connect() as connection:
                duts = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT dut_name, owner_job_id, created_at, heartbeat_at "
                        "FROM dut_locks ORDER BY dut_name"
                    ).fetchall()
                ]
                builders = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT builder_name, owner_job_id, created_at, heartbeat_at "
                        "FROM builder_locks ORDER BY builder_name"
                    ).fetchall()
                ]
        except sqlite3.Error:
            return  # snapshot is best-effort; never fail the caller
        snapshot_path = self.path.parent / "locks.json"
        existing_extra = _read_non_sql_locks(snapshot_path)
        payload = {
            "generated_at": _now(),
            "dut_locks": duts,
            "builder_locks": builders,
            "serial_locks": existing_extra["serial_locks"],
            "artifact_locks": existing_extra["artifact_locks"],
        }
        tmp_path = snapshot_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(snapshot_path)
        except OSError:
            # Snapshot is best-effort; SQLite remains the source of truth.
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def record_test_result(
        self,
        *,
        job_id: str,
        command: str,
        passed: bool,
        output: str,
        duration_sec: float,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO test_results (
                  job_id, command, passed, output, duration_sec, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, command, int(passed), output, duration_sec, _now()),
            )

    def recent_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, state, result, config_path, artifact_dir, started_at, finished_at, pid
                  FROM jobs
                 ORDER BY started_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, state, result, config_path, artifact_dir,
                       started_at, finished_at, config_snapshot, pid
                  FROM jobs
                 WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        if record.get("config_snapshot"):
            record["config_snapshot"] = json.loads(record["config_snapshot"])
        return record

    def last_successful_job(self, *, exclude_id: str | None = None) -> dict[str, Any] | None:
        """Return the most-recently-finished SUCCEEDED job, with config_snapshot
        already JSON-decoded. Excludes the optional `exclude_id` so a freshly-
        created in-progress job doesn't compare against itself.
        """
        with self._connect() as connection:
            if exclude_id is None:
                row = connection.execute(
                    """
                    SELECT id, state, result, config_path, artifact_dir,
                           started_at, finished_at, config_snapshot, pid
                      FROM jobs
                     WHERE result = 'success'
                     ORDER BY finished_at DESC
                     LIMIT 1
                    """
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT id, state, result, config_path, artifact_dir,
                           started_at, finished_at, config_snapshot, pid
                      FROM jobs
                     WHERE result = 'success' AND id != ?
                     ORDER BY finished_at DESC
                     LIMIT 1
                    """,
                    (exclude_id,),
                ).fetchone()
        if row is None:
            return None
        record = dict(row)
        if record.get("config_snapshot"):
            record["config_snapshot"] = json.loads(record["config_snapshot"])
        return record

    def recent_metrics(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most-recent jobs' result + metrics for trend analysis.

        Each entry is `{id, result, started_at, metrics}` — `metrics` is the
        decoded JSON dict (empty when the job didn't record any).
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, result, started_at, metrics
                  FROM jobs
                 ORDER BY started_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            metrics_raw = entry.get("metrics")
            entry["metrics"] = json.loads(metrics_raw) if metrics_raw else {}
            out.append(entry)
        return out

    def get_latest_artifact(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT container_path, host_path, filename, size_bytes, sha256
                  FROM artifacts
                 WHERE job_id = ?
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_job_pid(self, *, job_id: str, pid: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET pid = ? WHERE id = ?",
                (pid, job_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        statements: Iterable[str] = [
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              config_path TEXT NOT NULL,
              artifact_dir TEXT NOT NULL,
              state TEXT NOT NULL,
              result TEXT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              config_snapshot TEXT NOT NULL,
              pid INTEGER,
              metrics TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS job_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              ts TEXT NOT NULL,
              level TEXT NOT NULL,
              component TEXT NOT NULL,
              event TEXT NOT NULL,
              message TEXT NOT NULL,
              fields TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS artifacts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              container_path TEXT NOT NULL,
              host_path TEXT NOT NULL,
              filename TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dut_locks (
              dut_name TEXT PRIMARY KEY,
              owner_job_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS builder_locks (
              builder_name TEXT PRIMARY KEY,
              owner_job_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS test_results (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              command TEXT NOT NULL,
              passed INTEGER NOT NULL,
              output TEXT NOT NULL,
              duration_sec REAL NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_job_events_job_id
              ON job_events(job_id, ts)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_test_results_job_id
              ON test_results(job_id, created_at)
            """,
        ]
        with self._connect() as connection:
            for statement in statements:
                connection.execute(statement)
            self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        existing = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
        if "pid" not in existing:
            connection.execute("ALTER TABLE jobs ADD COLUMN pid INTEGER")
        if "metrics" not in existing:
            connection.execute("ALTER TABLE jobs ADD COLUMN metrics TEXT")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_non_sql_locks(snapshot_path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"serial_locks": [], "artifact_locks": []}
    out: dict[str, list[dict[str, Any]]] = {}
    for key in ("serial_locks", "artifact_locks"):
        value = payload.get(key)
        out[key] = value if isinstance(value, list) else []
    return out


def _is_stale(heartbeat_iso: str, lock_timeout_sec: int) -> bool:
    try:
        heartbeat = datetime.fromisoformat(heartbeat_iso)
    except ValueError:
        return True
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    threshold = datetime.now(UTC) - timedelta(seconds=lock_timeout_sec)
    return heartbeat < threshold
