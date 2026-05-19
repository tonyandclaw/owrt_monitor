from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from owrt_monitor.storage import JobStore


@dataclass(frozen=True)
class JobInspection:
    """Combined view of a job from the SQLite record and its report.json.

    Fields default to None / {} when the corresponding source is missing —
    e.g. an older job that predates a particular feature won't have
    `dut_status` or `metrics`.
    """

    job_id: str
    state: str
    result: str | None
    started_at: str
    finished_at: str | None
    pid: int | None
    run_dir: Path
    artifact: dict[str, Any] | None = None
    build_summary: dict[str, Any] | None = None
    build_metadata: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    dut_status: dict[str, Any] | None = None
    test_results: list[dict[str, Any]] | None = None
    script_results: list[dict[str, Any]] | None = None
    pytest_results: list[dict[str, Any]] | None = None
    ssh_results: list[dict[str, Any]] | None = None
    warnings: list[str] | None = None
    actions: list[str] | None = None


def inspect_job(store: JobStore, job_id: str) -> JobInspection | None:
    """Look up a job in SQLite and merge with its on-disk report.json.

    Returns None when the job_id isn't in the DB. A missing or unreadable
    report.json yields an inspection with the report-side fields left as None
    rather than raising — the DB record alone is still useful.
    """
    record = store.get_job(job_id)
    if record is None:
        return None

    run_dir = Path(record["artifact_dir"])
    report = _load_report_json(run_dir)
    return JobInspection(
        job_id=record["id"],
        state=record["state"],
        result=record.get("result"),
        started_at=record["started_at"],
        finished_at=record.get("finished_at"),
        pid=record.get("pid"),
        run_dir=run_dir,
        artifact=report.get("artifact") if report else None,
        build_summary=report.get("build_summary") if report else None,
        build_metadata=report.get("build_metadata") if report else None,
        metrics=report.get("metrics") if report else None,
        dut_status=report.get("dut_status") if report else None,
        test_results=report.get("test_results") if report else None,
        script_results=report.get("script_results") if report else None,
        pytest_results=report.get("pytest_results") if report else None,
        ssh_results=report.get("ssh_results") if report else None,
        warnings=report.get("warnings") if report else None,
        actions=report.get("actions") if report else None,
    )


def _load_report_json(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "report.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def diff_pairs(left: JobInspection, right: JobInspection) -> list[tuple[str, str, str]]:
    """Return `(label, left_value, right_value)` triples for everything worth
    comparing across two jobs. Stable order so callers can render side-by-side.
    Values are stringified — None / missing renders as a sentinel.
    """
    pairs: list[tuple[str, str, str]] = []

    def add(label: str, lv: object, rv: object) -> None:
        pairs.append((label, _format_value(lv), _format_value(rv)))

    add("state", left.state, right.state)
    add("result", left.result, right.result)
    add("started_at", left.started_at, right.started_at)
    add("finished_at", left.finished_at, right.finished_at)

    # Artifact identity
    la = left.artifact or {}
    ra = right.artifact or {}
    add("artifact.filename", la.get("filename"), ra.get("filename"))
    add("artifact.size_bytes", la.get("size_bytes"), ra.get("size_bytes"))
    add("artifact.sha256", la.get("sha256"), ra.get("sha256"))

    # Provenance
    lm = left.build_metadata or {}
    rm = right.build_metadata or {}
    for key in ("profile", "make_target", "git_commit", "git_describe", "git_dirty", "built_at"):
        add(f"provenance.{key}", lm.get(key), rm.get(key))

    # Build summary
    ls = left.build_summary or {}
    rs = right.build_summary or {}
    add("build.classification", ls.get("classification"), rs.get("classification"))
    add("build.duration_sec", ls.get("duration_sec"), rs.get("duration_sec"))
    add("build.failed_package", ls.get("failed_package"), rs.get("failed_package"))
    add("build.warnings", _safe_len(ls.get("warnings")), _safe_len(rs.get("warnings")))

    # Metrics
    lmet = left.metrics or {}
    rmet = right.metrics or {}
    for key in (
        "build_duration_sec",
        "boot_duration_sec",
        "flash_duration_sec",
        "test_duration_sec",
        "smoke_duration_sec",
        "script_duration_sec",
        "pytest_duration_sec",
        "ssh_duration_sec",
    ):
        add(f"metrics.{key}", lmet.get(key), rmet.get(key))

    # DUT status
    lds = left.dut_status or {}
    rds = right.dut_status or {}
    add("dut.kernel", lds.get("kernel"), rds.get("kernel"))
    add("dut.hostname", lds.get("hostname"), rds.get("hostname"))
    add("dut.board", lds.get("board"), rds.get("board"))
    lrel = (lds.get("release") or {}) if lds else {}
    rrel = (rds.get("release") or {}) if rds else {}
    add("dut.release.distribution", lrel.get("distribution"), rrel.get("distribution"))
    add("dut.release.version", lrel.get("version"), rrel.get("version"))
    add("dut.release.revision", lrel.get("revision"), rrel.get("revision"))

    # Test result summaries
    add("smoke.results", _result_summary(left.test_results), _result_summary(right.test_results))
    add(
        "scripts.results",
        _result_summary(left.script_results),
        _result_summary(right.script_results),
    )
    add(
        "pytest.results",
        _result_summary(left.pytest_results),
        _result_summary(right.pytest_results),
    )
    add("ssh.results", _result_summary(left.ssh_results), _result_summary(right.ssh_results))
    return pairs


def _format_value(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _safe_len(value: object) -> int | None:
    if isinstance(value, list):
        return len(value)
    return None


def _result_summary(results: list[dict[str, Any]] | None) -> str:
    if not results:
        return "—"
    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    skipped = sum(1 for r in results if r.get("skipped"))
    suffix = f", {skipped} skipped" if skipped else ""
    return f"{passed}/{total}{suffix}"
