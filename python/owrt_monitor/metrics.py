from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DurationStats:
    count: int = 0
    mean: float = 0.0
    median: float = 0.0
    p90: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0


@dataclass(frozen=True)
class MetricsSummary:
    total_jobs: int = 0
    counts_by_result: dict[str, int] = field(default_factory=dict)
    success_rate: float | None = None  # success / (success + failed); None if no terminal jobs
    durations: dict[str, DurationStats] = field(default_factory=dict)


def aggregate_metrics(rows: Iterable[dict[str, Any]]) -> MetricsSummary:
    """Aggregate `JobStore.recent_metrics` rows into a MetricsSummary.

    Robust to missing/None metric values: jobs without a given metric are
    excluded from that metric's stats but still counted in totals.
    """
    rows_list = list(rows)
    counts: Counter[str] = Counter()
    durations_buckets: dict[str, list[float]] = {}

    for row in rows_list:
        result = row.get("result") or "in_progress"
        counts[result] += 1
        metrics = row.get("metrics") or {}
        for key, value in metrics.items():
            if not isinstance(value, (int, float)) or math.isnan(float(value)):
                continue
            durations_buckets.setdefault(key, []).append(float(value))

    success = counts.get("success", 0)
    failed = counts.get("failed", 0)
    success_rate = success / (success + failed) if (success + failed) else None

    durations: dict[str, DurationStats] = {}
    for key, values in durations_buckets.items():
        durations[key] = _stats_for(values)

    return MetricsSummary(
        total_jobs=len(rows_list),
        counts_by_result=dict(counts),
        success_rate=success_rate,
        durations=durations,
    )


def _stats_for(values: list[float]) -> DurationStats:
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 0:
        return DurationStats()
    mean = sum(sorted_values) / n
    median = _percentile(sorted_values, 50)
    p90 = _percentile(sorted_values, 90)
    return DurationStats(
        count=n,
        mean=mean,
        median=median,
        p90=p90,
        minimum=sorted_values[0],
        maximum=sorted_values[-1],
    )


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Linear-interpolation percentile (matches numpy default).

    Always called with sorted, non-empty input.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (percentile / 100) * (len(sorted_values) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return sorted_values[low]
    fraction = rank - low
    return sorted_values[low] + fraction * (sorted_values[high] - sorted_values[low])
