from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
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
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                  id, config_path, artifact_dir, state, result,
                  started_at, finished_at, config_snapshot
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?)
                """,
                (
                    job_id,
                    str(config_path),
                    str(artifact_dir),
                    state,
                    _now(),
                    json.dumps(config_snapshot, sort_keys=True),
                ),
            )

    def update_job(self, *, job_id: str, state: str, result: str | None = None) -> None:
        finished_at = _now() if result is not None else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                   SET state = ?,
                       result = COALESCE(?, result),
                       finished_at = COALESCE(?, finished_at)
                 WHERE id = ?
                """,
                (state, result, finished_at, job_id),
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

    def acquire_dut_lock(self, *, dut_name: str, owner_job_id: str) -> bool:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO dut_locks (dut_name, owner_job_id, created_at, heartbeat_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (dut_name, owner_job_id, _now(), _now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_dut_lock(self, *, dut_name: str, owner_job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM dut_locks
                 WHERE dut_name = ? AND owner_job_id = ?
                """,
                (dut_name, owner_job_id),
            )

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
                SELECT id, state, result, config_path, artifact_dir, started_at, finished_at
                  FROM jobs
                 ORDER BY started_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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
              config_snapshot TEXT NOT NULL
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


def _now() -> str:
    return datetime.now(UTC).isoformat()
