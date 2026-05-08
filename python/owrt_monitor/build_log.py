from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# enw-device-firmware top-level Makefile prints this on a successful build.
# Example: ">>>> owrt2102.asus_mt_wifi7_mt7987  Build done in: 05:07.537"
_SUCCESS_DONE = re.compile(r"^>>>> (?P<target>\S+)\s+Build done in:\s+(?P<duration>[\d:\.]+)\s*$")

# `No space left on device` is high-signal: if it appears anywhere in the log we
# call this disk_full regardless of any subsequent generic "Error 2" lines, since
# downstream errors are consequences of the disk being full.
_DISK_FULL = re.compile(r"No space left on device", re.IGNORECASE)

# Final make target that failed at the top level — the profile name itself.
# Example: "make: *** [include/owrt2102.mk:163: owrt2102.asus_mt_wifi7_mt7987] Error 2"
_TOPLEVEL_FAIL = re.compile(
    r"^make:\s+\*\*\*\s+\[(?P<makefile>[^\]]+):\s*(?P<target>\S+)\]\s+Error\s+\d+\s*$"
)

# A package or subdirectory that failed during the build, e.g.
# "make[3]: *** [package/feeds/mtk/foo/compile] Error 2"
_PACKAGE_FAIL = re.compile(
    r"^make\[\d+\]:\s+\*\*\*\s+\[(?P<step>[^\]]+)\]\s+Error\s+\d+\s*$"
)

# Non-fatal warnings emitted by OpenWrt's package metadata pass.
_WARNING = re.compile(r"^WARNING:\s+(?P<message>.*)$")

# Noise lines we should ignore when picking evidence (they almost always appear
# because the firmware tree calls `git describe` for versioning):
_KNOWN_NOISE_PATTERNS = (
    re.compile(r"^fatal: No names found, cannot describe anything\.\s*$"),
    re.compile(r"^cat: write error: No space left on device\s*$"),  # not the canonical signal
)

_MAX_WARNINGS = 50
_MAX_EVIDENCE_LINES = 5


@dataclass(frozen=True)
class BuildLogSummary:
    classification: str
    success: bool
    duration_sec: float | None = None
    failed_target: str | None = None
    failed_step: str | None = None
    failed_package: str | None = None
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "success": self.success,
            "duration_sec": self.duration_sec,
            "failed_target": self.failed_target,
            "failed_step": self.failed_step,
            "failed_package": self.failed_package,
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
        }


def _extract_failed_package(step: str | None) -> str | None:
    """Derive a human-readable package name from a make step path.

    Examples:
      package/foo/bar/compile          → foo/bar
      package/feeds/mtk/flowtable/install → feeds/mtk/flowtable
      target/linux/install             → target/linux
      /abs/path/...mk:228: world       → None  (synthetic toplevel-mk target, not a package)
    """
    if not step:
        return None
    # Synthetic make targets that aren't real packages.
    if step.endswith("world") or "toplevel.mk" in step:
        return None
    # Strip the trailing make target (compile/install/configure/etc).
    parts = step.split("/")
    if len(parts) < 2:
        return None
    leaf_targets = {"compile", "install", "configure"}
    if parts[0] == "package":
        package_parts = parts[1:-1] if parts[-1] in leaf_targets else parts[1:]
        return "/".join(package_parts) if package_parts else None
    if parts[0] == "target":
        package_parts = parts[1:-1] if parts[-1] in leaf_targets else parts[1:]
        return "target/" + "/".join(package_parts) if package_parts else None
    return None


def classify_build_log(log_path: Path) -> BuildLogSummary:
    """Classify a build.log into a structured summary.

    Reads the entire file (build logs for this lab top out at ~tens of KB).
    """
    if not log_path.exists():
        return BuildLogSummary(classification="missing_log", success=False)

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return BuildLogSummary(
            classification="unreadable_log",
            success=False,
            evidence=[str(exc)],
        )

    success_match: re.Match[str] | None = None
    toplevel_fail: re.Match[str] | None = None
    package_fail: re.Match[str] | None = None
    disk_full_lines: list[str] = []
    warnings: list[str] = []

    for line in lines:
        m_success = _SUCCESS_DONE.match(line)
        if m_success is not None:
            success_match = m_success
            continue

        if _DISK_FULL.search(line) and not any(p.match(line) for p in _KNOWN_NOISE_PATTERNS):
            disk_full_lines.append(line)
            continue

        m_top = _TOPLEVEL_FAIL.match(line)
        if m_top is not None:
            toplevel_fail = m_top
            continue

        m_pkg = _PACKAGE_FAIL.match(line)
        if m_pkg is not None:
            # Keep the FIRST match: in make output the deepest failure is reported
            # first, with outer make levels appending propagation lines. The first
            # `make[N]: *** [...] Error N` is therefore the actual root cause.
            if package_fail is None:
                package_fail = m_pkg
            continue

        m_warn = _WARNING.match(line)
        if m_warn is not None and len(warnings) < _MAX_WARNINGS:
            warnings.append(m_warn.group("message"))

    if success_match is not None:
        duration = _parse_duration(success_match.group("duration"))
        return BuildLogSummary(
            classification="success",
            success=True,
            duration_sec=duration,
            failed_target=None,
            failed_step=None,
            evidence=[success_match.group(0)],
            warnings=warnings,
        )

    if disk_full_lines:
        evidence = disk_full_lines[:_MAX_EVIDENCE_LINES]
        return BuildLogSummary(
            classification="disk_full",
            success=False,
            failed_target=toplevel_fail.group("target") if toplevel_fail else None,
            evidence=evidence,
            warnings=warnings,
        )

    if package_fail is not None:
        step = package_fail.group("step")
        return BuildLogSummary(
            classification="failed_package",
            success=False,
            failed_target=toplevel_fail.group("target") if toplevel_fail else None,
            failed_step=step,
            failed_package=_extract_failed_package(step),
            evidence=[package_fail.group(0)],
            warnings=warnings,
        )

    if toplevel_fail is not None:
        return BuildLogSummary(
            classification="compile_error",
            success=False,
            failed_target=toplevel_fail.group("target"),
            evidence=[toplevel_fail.group(0)],
            warnings=warnings,
        )

    # No success marker, no recognised failure marker. Could be a partial log
    # (process killed mid-stream) or a wholly unrecognised failure shape.
    tail_evidence = [
        line for line in lines[-_MAX_EVIDENCE_LINES:] if line.strip()
    ]
    return BuildLogSummary(
        classification="unknown",
        success=False,
        evidence=tail_evidence,
        warnings=warnings,
    )


def _parse_duration(value: str) -> float | None:
    """Parse 'MM:SS.fff' or 'HH:MM:SS.fff' into seconds."""
    parts = value.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None
    return None
