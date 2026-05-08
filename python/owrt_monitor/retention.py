from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass
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
    artifact_root: Path | None = None,
    limit: int = 1000,
) -> PrunePlan:
    """Decide which run_dirs to remove based on the keep counts.

    For each result bucket (success / failed / other), keep the newest N jobs
    by `started_at`; everything older becomes a target. `artifact_root` is
    optional — when set, only jobs whose `artifact_dir` is inside it are
    considered (defends against pruning unrelated jobs from a shared DB).
    """
    rows = store.recent_jobs(limit=limit)
    by_result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if artifact_root is not None:
            try:
                Path(row["artifact_dir"]).resolve().relative_to(artifact_root.resolve())
            except (ValueError, OSError):
                continue
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
            run_dir = Path(row["artifact_dir"])
            if not run_dir.is_dir():
                continue
            size = _dir_size(run_dir)
            targets.append(
                PruneTarget(
                    job_id=row["id"],
                    result=result,
                    started_at=row["started_at"],
                    run_dir=run_dir,
                    size_bytes=size,
                )
            )
            total_bytes += size
    targets.sort(key=lambda t: t.started_at)  # oldest first → readable output
    return PrunePlan(
        targets=targets,
        total_bytes=total_bytes,
        kept_count_by_result=dict(kept_counts),
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
