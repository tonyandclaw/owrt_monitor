from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from owrt_monitor.artifacts import ExportedArtifact


@dataclass
class WorkflowReport:
    job_id: str
    state: str
    success: bool
    dry_run: bool
    run_dir: Path
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifact: ExportedArtifact | None = None
    test_results: list[dict[str, Any]] = field(default_factory=list)
    build_summary: dict[str, Any] | None = None
    build_metadata: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    dut_status: dict[str, Any] | None = None
    script_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["run_dir"] = str(self.run_dir)
        if self.artifact is not None:
            data["artifact"] = {
                "container_path": self.artifact.container_path,
                "host_path": str(self.artifact.host_path),
                "filename": self.artifact.filename,
                "size_bytes": self.artifact.size_bytes,
                "sha256": self.artifact.sha256,
            }
        return data


def write_config_snapshot(path: Path, config_dump: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config_dump, sort_keys=False), encoding="utf-8")


def write_report(report: WorkflowReport) -> None:
    report.run_dir.mkdir(parents=True, exist_ok=True)
    data = report.to_dict()
    (report.run_dir / "report.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (report.run_dir / "report.md").write_text(_markdown_report(data), encoding="utf-8")


def _markdown_report(data: dict[str, Any]) -> str:
    lines = [
        f"# owrt_monitor job {data['job_id']}",
        "",
        f"- State: `{data['state']}`",
        f"- Success: `{data['success']}`",
        f"- Dry run: `{data['dry_run']}`",
        f"- Run directory: `{data['run_dir']}`",
    ]

    artifact = data.get("artifact")
    if artifact:
        lines.extend(
            [
                "",
                "## Artifact",
                "",
                f"- File: `{artifact['filename']}`",
                f"- Host path: `{artifact['host_path']}`",
                f"- Container path: `{artifact['container_path']}`",
                f"- Size bytes: `{artifact['size_bytes']}`",
                f"- SHA256: `{artifact['sha256']}`",
            ]
        )

    metadata = data.get("build_metadata")
    if metadata:
        lines.extend(["", "## Provenance", ""])
        ordered_keys = (
            "built_at",
            "make_target",
            "profile",
            "git_commit",
            "git_describe",
            "git_dirty",
        )
        for key in ordered_keys:
            if key in metadata and metadata[key] is not None:
                lines.append(f"- {key}: `{metadata[key]}`")
        # Surface any extra keys we did not enumerate above.
        for key, value in metadata.items():
            if key not in ordered_keys and value is not None:
                lines.append(f"- {key}: `{value}`")

    if data["actions"]:
        lines.extend(["", "## Actions", ""])
        lines.extend(f"- {action}" for action in data["actions"])

    if data["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in data["warnings"])

    dut_status = data.get("dut_status")
    if dut_status:
        lines.extend(["", "## DUT Status", ""])
        if dut_status.get("parse_error"):
            lines.append(f"- Parse error: `{dut_status['parse_error']}`")
        for key in ("hostname", "model", "board", "kernel"):
            value = dut_status.get(key)
            if value is not None:
                lines.append(f"- {key}: `{value}`")
        release = dut_status.get("release") or {}
        if release:
            for key in ("distribution", "version", "revision", "target", "description"):
                if key in release and release[key] is not None:
                    lines.append(f"- release.{key}: `{release[key]}`")

    metrics = data.get("metrics")
    if metrics:
        lines.extend(["", "## Metrics", ""])
        ordered = (
            "build_duration_sec",
            "transfer_duration_sec",
            "boot_duration_sec",
            "smoke_duration_sec",
            "total_duration_sec",
        )
        for key in ordered:
            if key in metrics and metrics[key] is not None:
                lines.append(f"- {key}: `{float(metrics[key]):.2f} s`")
        # Surface any extras we didn't enumerate (forward-compatible).
        for key, value in metrics.items():
            if key not in ordered and value is not None:
                lines.append(f"- {key}: `{value}`")

    summary = data.get("build_summary")
    if summary:
        lines.extend(["", "## Build Log", ""])
        lines.append(f"- Classification: `{summary['classification']}`")
        if summary.get("duration_sec") is not None:
            lines.append(f"- Build duration: `{summary['duration_sec']:.1f} s`")
        if summary.get("failed_target"):
            lines.append(f"- Failed target: `{summary['failed_target']}`")
        if summary.get("failed_package"):
            lines.append(f"- Failed package: `{summary['failed_package']}`")
        if summary.get("failed_step"):
            lines.append(f"- Failed step: `{summary['failed_step']}`")
        if summary.get("evidence"):
            lines.extend(["", "### Evidence", "", "```"])
            lines.extend(summary["evidence"])
            lines.append("```")
        if summary.get("warnings"):
            lines.extend(["", "### Build warnings", ""])
            lines.extend(f"- {w}" for w in summary["warnings"])

    script_results = data.get("script_results") or []
    if script_results:
        passed = sum(1 for r in script_results if r["passed"])
        total = len(script_results)
        verdict = "PASS" if passed == total else "FAIL"
        lines.extend([
            "",
            "## Custom Scripts",
            "",
            f"- Result: **{verdict}** ({passed}/{total} passed)",
            "",
        ])
        for r in script_results:
            status = "passed" if r["passed"] else "failed"
            timeout_marker = " (TIMEOUT)" if r.get("timed_out") else ""
            lines.append(
                f"- `{r['name']}` [{r['path']}] exit={r['exit_code']}: "
                f"{status}{timeout_marker} ({r['duration_sec']:.2f} s)"
            )

    if data["test_results"]:
        results = data["test_results"]
        passed = sum(1 for r in results if r["passed"])
        total = len(results)
        total_duration = sum(float(r.get("duration_sec") or 0) for r in results)
        verdict = "PASS" if passed == total else "FAIL"
        lines.extend(
            [
                "",
                "## Smoke Tests",
                "",
                f"- Result: **{verdict}** ({passed}/{total} passed, "
                f"{total - passed} failed, {total_duration:.1f} s total)",
                "",
            ]
        )
        for result in results:
            status = "passed" if result["passed"] else "failed"
            duration = result.get("duration_sec") or 0
            lines.append(f"- `{result['command']}`: {status} ({float(duration):.2f} s)")

    return "\n".join(lines) + "\n"
