from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from owrt_monitor.storage import JobStore


@dataclass(frozen=True)
class PruneTarget:
    """One run directory eligible for deletion."""

    job_id: str
    result: str
    started_at: str
    run_dir: Path
    size_bytes: int


@dataclass(frozen=True)
class PrunePlan:
    """The result of `plan_prune` — what would be deleted, what would be kept."""

    targets: list[PruneTarget]
    total_bytes: int
    kept_count_by_result: dict[str, int]


def plan_prune(
    store: JobStore,
    *,
    keep_success: int = 10,
    keep_failed: int = 5,
    keep_other: int = 5,
    max_age_days: int | None = None,
    now: datetime | None = None,
    artifact_root: Path | None = None,
    limit: int = 1000,
) -> PrunePlan:
    """Decide which run_dirs to remove.

    Two mutually-exclusive modes:

    * **Age-based** (when `max_age_days` is set): every job whose `started_at`
      is older than `max_age_days` becomes a target, regardless of result or
      the keep counts. Jobs with an unparseable `started_at` are kept (we never
      delete something of unknown age). `now` is injectable for testing.
    * **Count-based** (default): for each result bucket (success / failed /
      other), keep the newest N jobs by `started_at`; everything older becomes a
      target.

    `artifact_root` is optional — when set, only jobs whose `artifact_dir` is
    inside it are considered (defends against pruning unrelated jobs from a
    shared DB).
    """
    rows = store.recent_jobs(limit=limit)
    if artifact_root is not None:
        root = artifact_root.resolve()
        scoped: list[dict[str, Any]] = []
        for row in rows:
            try:
                Path(row["artifact_dir"]).resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            scoped.append(row)
        rows = scoped

    if max_age_days is not None:
        return _plan_by_age(rows, max_age_days=max_age_days, now=now)
    return _plan_by_count(
        rows,
        keep_success=keep_success,
        keep_failed=keep_failed,
        keep_other=keep_other,
    )


def _plan_by_count(
    rows: list[dict[str, Any]],
    *,
    keep_success: int,
    keep_failed: int,
    keep_other: int,
) -> PrunePlan:
    by_result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result = row.get("result") or "in_progress"
        by_result[result].append(row)

    keep_table = {"success": keep_success, "failed": keep_failed}
    targets: list[PruneTarget] = []
    kept_counts: dict[str, int] = {}
    total_bytes = 0
    for result, entries in by_result.items():
        # `recent_jobs` is already sorted newest-first.
        keep = keep_table.get(result, keep_other)
        kept_counts[result] = min(keep, len(entries))
        for row in entries[keep:]:
            target = _make_target(row, result)
            if target is None:
                continue
            targets.append(target)
            total_bytes += target.size_bytes
    targets.sort(key=lambda t: t.started_at)  # oldest first → readable output
    return PrunePlan(
        targets=targets,
        total_bytes=total_bytes,
        kept_count_by_result=dict(kept_counts),
    )


def _plan_by_age(
    rows: list[dict[str, Any]],
    *,
    max_age_days: int,
    now: datetime | None,
) -> PrunePlan:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=max_age_days)
    targets: list[PruneTarget] = []
    kept_counts: dict[str, int] = defaultdict(int)
    total_bytes = 0
    for row in rows:
        result = row.get("result") or "in_progress"
        started = _parse_started_at(row.get("started_at"))
        # Safety: keep anything newer than the cutoff, and keep anything whose
        # timestamp we cannot parse rather than risk deleting it.
        if started is None or started >= cutoff:
            kept_counts[result] += 1
            continue
        target = _make_target(row, result)
        if target is None:
            continue
        targets.append(target)
        total_bytes += target.size_bytes
    targets.sort(key=lambda t: t.started_at)  # oldest first → readable output
    return PrunePlan(
        targets=targets,
        total_bytes=total_bytes,
        kept_count_by_result=dict(kept_counts),
    )


def _parse_started_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _make_target(row: dict[str, Any], result: str) -> PruneTarget | None:
    run_dir = Path(row["artifact_dir"])
    if not run_dir.is_dir():
        return None
    return PruneTarget(
        job_id=row["id"],
        result=result,
        started_at=row["started_at"],
        run_dir=run_dir,
        size_bytes=_dir_size(run_dir),
    )


def apply_prune(targets: list[PruneTarget]) -> int:
    """Delete each target's run_dir. Returns the total bytes actually freed.

    `shutil.rmtree(ignore_errors=False)` so a permission failure surfaces
    rather than silently leaving partial trees behind. Caller should plan
    around that — usually a fresh planning pass after the failure.
    """
    freed = 0
    for target in targets:
        if not target.run_dir.is_dir():
            continue
        shutil.rmtree(target.run_dir, ignore_errors=False)
        freed += target.size_bytes
    return freed


def _dir_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def format_bytes(num: int) -> str:
    """Compact human-friendly size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}" if unit != "B" else f"{num} {unit}"
        num /= 1024
    return f"{num:.1f} PB"
