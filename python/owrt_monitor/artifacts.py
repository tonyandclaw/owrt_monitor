from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class ArtifactSelectionError(RuntimeError):
    """Raised when firmware artifact selection cannot produce one safe candidate."""


SelectionPolicy = Literal["newest", "largest", "fail-if-multiple"]


@dataclass(frozen=True)
class ArtifactCandidate:
    path: str
    size_bytes: int
    mtime: float

    @property
    def filename(self) -> str:
        return Path(self.path).name


@dataclass(frozen=True)
class ExportedArtifact:
    container_path: str
    host_path: Path
    filename: str
    size_bytes: int
    sha256: str


def select_artifact(
    candidates: list[ArtifactCandidate],
    *,
    selection: SelectionPolicy,
    min_size_mb: float = 0,
    regex_patterns: list[str] | None = None,
) -> ArtifactCandidate:
    if not candidates:
        raise ArtifactSelectionError("no artifacts matched the configured artifact patterns")

    minimum_bytes = int(min_size_mb * 1024 * 1024)
    eligible = [candidate for candidate in candidates if candidate.size_bytes >= minimum_bytes]

    if regex_patterns:
        compiled = [re.compile(p) for p in regex_patterns]
        eligible = [c for c in eligible if all(r.search(c.path) for r in compiled)]
        if not eligible:
            raise ArtifactSelectionError(
                f"no firmware artifacts matched all regex_patterns: {regex_patterns!r}"
            )

    if not eligible:
        raise ArtifactSelectionError(
            f"no firmware artifacts matched the size threshold ({min_size_mb:g} MB)"
        )

    if selection == "newest":
        return max(eligible, key=lambda candidate: (candidate.mtime, candidate.size_bytes))
    if selection == "largest":
        return max(eligible, key=lambda candidate: (candidate.size_bytes, candidate.mtime))
    if selection == "fail-if-multiple":
        if len(eligible) != 1:
            paths = ", ".join(candidate.path for candidate in eligible)
            raise ArtifactSelectionError(f"expected one artifact, found {len(eligible)}: {paths}")
        return eligible[0]

    raise ArtifactSelectionError(f"unsupported artifact selection policy: {selection}")
