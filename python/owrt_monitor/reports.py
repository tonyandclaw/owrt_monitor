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

    if data["actions"]:
        lines.extend(["", "## Actions", ""])
        lines.extend(f"- {action}" for action in data["actions"])

    if data["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in data["warnings"])

    if data["test_results"]:
        lines.extend(["", "## Smoke Tests", ""])
        for result in data["test_results"]:
            status = "passed" if result["passed"] else "failed"
            lines.append(f"- `{result['command']}`: {status}")

    return "\n".join(lines) + "\n"
