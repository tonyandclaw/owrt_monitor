from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Sentinel — distinguishes "key present with value None" from "key absent".
_MISSING = object()


@dataclass(frozen=True)
class ConfigChange:
    """One difference between two redacted config snapshots.

    `path` is dotted (e.g. `tests.smoke[2].command`), `old` is the previous
    value, `new` is the new value, with `<missing>` placeholders when a side
    is absent (added or removed key).
    """

    path: str
    old: Any
    new: Any


def diff_configs(old: Any, new: Any, *, prefix: str = "") -> list[ConfigChange]:
    """Recursively diff two redacted config snapshots.

    Rules:
      - Dicts: recurse into common keys; keys on only one side become
        added/removed entries.
      - Lists: when lengths differ, record the whole list as one change.
        When equal, recurse element-wise (path uses `[i]` suffix).
      - Scalars: equal → skip, differ → one change.

    The output is sorted by path for stable display.
    """
    changes = _walk(old, new, prefix)
    changes.sort(key=lambda c: c.path)
    return changes


def _walk(old: Any, new: Any, prefix: str) -> list[ConfigChange]:
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[ConfigChange] = []
        for key in set(old) | set(new):
            sub_path = f"{prefix}.{key}" if prefix else key
            sub_old = old.get(key, _MISSING)
            sub_new = new.get(key, _MISSING)
            if sub_old is _MISSING:
                out.append(ConfigChange(sub_path, "<missing>", sub_new))
            elif sub_new is _MISSING:
                out.append(ConfigChange(sub_path, sub_old, "<missing>"))
            else:
                out.extend(_walk(sub_old, sub_new, sub_path))
        return out
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return [ConfigChange(prefix or "<root>", old, new)]
        out = []
        for index, (a, b) in enumerate(zip(old, new, strict=True)):
            out.extend(_walk(a, b, f"{prefix}[{index}]"))
        return out
    if old != new:
        return [ConfigChange(prefix or "<root>", old, new)]
    return []


@dataclass(frozen=True)
class ConfigDiffSummary:
    """Compact summary suitable for event payloads / report rendering."""

    total: int
    sample: list[ConfigChange] = field(default_factory=list)


def summarize(changes: list[ConfigChange], *, sample_limit: int = 20) -> ConfigDiffSummary:
    return ConfigDiffSummary(
        total=len(changes),
        sample=list(changes[:sample_limit]),
    )
