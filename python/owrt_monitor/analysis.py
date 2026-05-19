from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from owrt_monitor.build_log import classify_build_log

_SOURCE_FILES = (
    "report.json",
    "events.jsonl",
    "runner.output.jsonl",
    "build.log",
    "serial.log",
)
_SECRET_REDACTIONS = (
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key)"
            r"(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*:\s*)(bearer|basic)\s+[^\s,;]+"),
        r"\1<redacted>",
    ),
)


def analyze_run_dir(run_dir: Path, *, max_tail_lines: int = 40) -> dict[str, Any]:
    """Build a deterministic advisory analysis from already persisted artifacts.

    This is intentionally not an LLM call. It produces the structured, redacted
    input bundle that a future LLM/UI layer can consume while keeping the
    deterministic workflow as the authority.
    """
    run_dir = run_dir.resolve()
    report = _load_json(run_dir / "report.json") or {}
    build_summary = _build_summary(run_dir, report)
    findings = _findings(report, build_summary, run_dir)
    next_actions = _next_actions(report, build_summary, findings)
    verdict = _verdict(report, build_summary, findings)
    source_files = _source_file_records(run_dir)
    evidence = {
        "build_summary": _build_evidence(run_dir, build_summary),
        "events_tail": _tail_evidence(run_dir / "events.jsonl", max_tail_lines),
        "runner_output_tail": _tail_evidence(
            run_dir / "runner.output.jsonl",
            max_tail_lines,
        ),
        "build_log_tail": _tail_evidence(run_dir / "build.log", max_tail_lines),
        "serial_log_tail": _tail_evidence(run_dir / "serial.log", max_tail_lines),
    }

    return {
        "schema_version": 1,
        "kind": "advisory_analysis",
        "generated_at": datetime.now(UTC).isoformat(),
        "job": {
            "job_id": str(report.get("job_id") or run_dir.name),
            "run_dir": str(run_dir),
            "state": report.get("state"),
            "success": report.get("success"),
            "dry_run": report.get("dry_run"),
            "result": _job_result(report),
        },
        "guardrails": {
            "advisory_only": True,
            "dangerous_actions_allowed": False,
            "input_policy": "structured_redacted_artifacts_only",
            "workflow_authority": "deterministic workflow and config",
            "approval_required_for": [
                "sysupgrade",
                "bootloader environment changes",
                "deleting build directories",
                "network changes on DUT",
            ],
        },
        "ui_summary": {
            "severity": _ui_severity(report, findings),
            "title": verdict["summary"],
            "badges": _ui_badges(report, build_summary, findings),
        },
        "verdict": verdict,
        "build": build_summary,
        "findings": findings,
        "next_actions": next_actions,
        "bug_report_draft": _bug_report_draft(
            report=report,
            build_summary=build_summary,
            verdict=verdict,
            findings=findings,
            next_actions=next_actions,
            source_files=source_files,
            evidence=evidence,
        ),
        "source_files": source_files,
        "evidence": evidence,
        "structured_input": _structured_input(report, build_summary),
    }


def write_analysis_files(run_dir: Path, analysis: dict[str, Any]) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "analysis.json"
    md_path = run_dir / "analysis.md"
    json_path.write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_analysis_markdown(analysis), encoding="utf-8")
    return json_path, md_path


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
    job = analysis.get("job") or {}
    verdict = analysis.get("verdict") or {}
    guardrails = analysis.get("guardrails") or {}
    lines = [
        f"# owrt_monitor advisory analysis {job.get('job_id', '')}",
        "",
        f"- Verdict: `{verdict.get('status', 'unknown')}`",
        f"- Summary: {verdict.get('summary', 'No summary available.')}",
        f"- Advisory only: `{guardrails.get('advisory_only')}`",
        f"- Dangerous actions allowed: `{guardrails.get('dangerous_actions_allowed')}`",
        f"- Workflow authority: `{guardrails.get('workflow_authority')}`",
    ]

    findings = analysis.get("findings") or []
    if findings:
        lines.extend(["", "## Findings", ""])
        for finding in findings:
            lines.append(
                f"- `{finding.get('severity', 'info')}` {finding.get('summary', '')}"
            )

    actions = analysis.get("next_actions") or []
    if actions:
        lines.extend(["", "## Next Actions", ""])
        for action in actions:
            lines.append(f"- {action}")

    evidence = (analysis.get("evidence") or {}).get("build_summary") or []
    if evidence:
        lines.extend(["", "## Build Evidence", ""])
        for item in evidence:
            location = f"{item.get('file')}:{item.get('line')}"
            lines.append(f"- `{location}` {item.get('text')}")

    draft = analysis.get("bug_report_draft") or {}
    if draft:
        lines.extend(
            [
                "",
                "## Bug Report Draft",
                "",
                f"Title: {draft.get('title', '')}",
                "",
                str(draft.get("body", "")),
            ]
        )

    source_files = analysis.get("source_files") or []
    if source_files:
        lines.extend(["", "## Source Files", ""])
        for source in source_files:
            lines.append(
                f"- `{source.get('path')}` sha256=`{source.get('sha256')}` "
                f"bytes=`{source.get('bytes')}`"
            )

    lines.append("")
    return "\n".join(lines)


def _build_summary(run_dir: Path, report: dict[str, Any]) -> dict[str, Any] | None:
    report_summary = report.get("build_summary")
    if isinstance(report_summary, dict):
        return report_summary
    build_log = run_dir / "build.log"
    if build_log.exists():
        return classify_build_log(build_log).to_dict()
    return None


def _findings(
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
    run_dir: Path,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if report.get("dry_run"):
        findings.append(
            {
                "code": "dry_run_only",
                "severity": "info",
                "summary": "This job was a dry-run; no build, flash, or DUT mutation was executed.",
            }
        )

    if build_summary:
        classification = str(build_summary.get("classification") or "unknown")
        if classification == "disk_full":
            findings.append(
                {
                    "code": "build_disk_full",
                    "severity": "error",
                    "summary": "OpenWrt build failed because the builder ran out of disk space.",
                }
            )
        elif classification == "failed_package":
            package = build_summary.get("failed_package") or build_summary.get("failed_step")
            findings.append(
                {
                    "code": "build_failed_package",
                    "severity": "error",
                    "summary": f"OpenWrt build failed inside `{package}`.",
                }
            )
        elif classification in {"compile_error", "unknown", "missing_log", "unreadable_log"}:
            findings.append(
                {
                    "code": f"build_{classification}",
                    "severity": "error",
                    "summary": f"Build did not succeed; classifier returned `{classification}`.",
                }
            )

    warnings = report.get("warnings") or []
    for warning in warnings[:10]:
        text = str(warning)
        code = "workflow_warning"
        severity = "warning"
        if "failed to boot" in text.lower() or "boot failure" in text.lower():
            code = "dut_boot_failure"
            severity = "error"
        findings.append(
            {
                "code": code,
                "severity": severity,
                "summary": _redact(text),
            }
        )

    for label, key in (
        ("smoke", "test_results"),
        ("script", "script_results"),
        ("pytest", "pytest_results"),
        ("ssh", "ssh_results"),
    ):
        failed = [
            result
            for result in report.get(key, []) or []
            if not result.get("passed") and not result.get("skipped")
        ]
        if failed:
            findings.append(
                {
                    "code": f"{label}_test_failed",
                    "severity": "error",
                    "summary": f"{len(failed)} {label} test result(s) failed.",
                }
            )

    if not findings and report.get("success") is True:
        findings.append(
            {
                "code": "job_succeeded",
                "severity": "info",
                "summary": "Job completed successfully according to report.json.",
            }
        )
    elif not findings and not (run_dir / "report.json").exists():
        findings.append(
            {
                "code": "missing_report",
                "severity": "error",
                "summary": "No report.json was found for this run directory.",
            }
        )
    return findings


def _next_actions(
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> list[str]:
    actions = [
        "Treat this analysis as advisory; rerun the deterministic command before acting.",
    ]
    codes = {str(finding.get("code")) for finding in findings}
    classification = (build_summary or {}).get("classification")
    if "build_disk_full" in codes:
        actions.append("Free space in the builder container/host volume, then rerun the build.")
    if "build_failed_package" in codes:
        package = (build_summary or {}).get("failed_package")
        if package:
            actions.append(f"Inspect the failing package `{package}` and rerun with verbose logs.")
        else:
            actions.append("Inspect the failing make step and rerun with verbose logs.")
    if classification in {"compile_error", "unknown"}:
        actions.append("Open build.log and runner.output.jsonl around the final error lines.")
    if "dut_boot_failure" in codes:
        actions.append("Inspect serial.log around the boot failure evidence before retrying flash.")
    if any(code.endswith("_test_failed") for code in codes):
        actions.append("Inspect failed test output in report.json, then rerun `owrt-monitor test`.")
    if report.get("dry_run"):
        actions.append("When the lab is ready, rerun without `--dry-run` and keep explicit guards.")
    if report.get("success") is not True and not report.get("dry_run"):
        actions.append("Do not run destructive flash based only on this analysis.")
    return actions


def _verdict(
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    if report.get("dry_run"):
        return {"status": "planned", "summary": "Dry-run completed; no mutation was executed."}
    if report.get("success") is True:
        return {"status": "succeeded", "summary": "Job succeeded."}
    errors = [finding for finding in findings if finding.get("severity") == "error"]
    if errors:
        return {"status": "failed", "summary": str(errors[0].get("summary"))}
    if build_summary and build_summary.get("success") is False:
        return {
            "status": "failed",
            "summary": f"Build classifier returned `{build_summary.get('classification')}`.",
        }
    return {"status": "unknown", "summary": "Not enough structured data to classify the job."}


def _structured_input(
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    artifact = report.get("artifact") or {}
    metrics = report.get("metrics") or {}
    metadata = report.get("build_metadata") or {}
    return {
        "report": {
            "state": report.get("state"),
            "success": report.get("success"),
            "dry_run": report.get("dry_run"),
            "warnings": [_redact(str(item)) for item in report.get("warnings", [])],
            "actions": [_redact(str(item)) for item in report.get("actions", [])],
        },
        "artifact": {
            "filename": artifact.get("filename"),
            "size_bytes": artifact.get("size_bytes"),
            "sha256": artifact.get("sha256"),
        },
        "build_summary": build_summary,
        "build_metadata": {
            "profile": metadata.get("profile"),
            "make_target": metadata.get("make_target"),
            "git_commit": metadata.get("git_commit"),
            "git_describe": metadata.get("git_describe"),
            "git_dirty": metadata.get("git_dirty"),
        },
        "metrics": metrics,
        "test_counts": {
            "smoke": _result_counts(report.get("test_results")),
            "scripts": _result_counts(report.get("script_results")),
            "pytest": _result_counts(report.get("pytest_results")),
            "ssh": _result_counts(report.get("ssh_results")),
        },
    }


def _bug_report_draft(
    *,
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
    verdict: dict[str, Any],
    findings: list[dict[str, Any]],
    next_actions: list[str],
    source_files: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    job_id = str(report.get("job_id") or "unknown-job")
    title = _bug_report_title(job_id, verdict, build_summary, findings)
    labels = _bug_report_labels(report, build_summary, findings)
    body = _bug_report_body(
        report=report,
        build_summary=build_summary,
        verdict=verdict,
        findings=findings,
        next_actions=next_actions,
        source_files=source_files,
        evidence=evidence,
    )
    return {
        "title": title,
        "labels": labels,
        "body": body,
    }


def _bug_report_title(
    job_id: str,
    verdict: dict[str, Any],
    build_summary: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> str:
    first_error = next(
        (finding for finding in findings if finding.get("severity") == "error"),
        None,
    )
    if first_error is not None:
        return _redact(f"owrt_monitor {job_id}: {first_error.get('summary')}")
    if build_summary and build_summary.get("classification"):
        return _redact(
            f"owrt_monitor {job_id}: build {build_summary.get('classification')}"
        )
    return _redact(f"owrt_monitor {job_id}: {verdict.get('status', 'unknown')}")


def _bug_report_labels(
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> list[str]:
    labels = ["owrt-monitor", "advisory-draft"]
    if report.get("dry_run"):
        labels.append("dry-run")
    if build_summary and build_summary.get("classification"):
        labels.append(f"build:{build_summary['classification']}")
    for finding in findings:
        code = finding.get("code")
        if isinstance(code, str) and code.startswith("dut_"):
            labels.append("dut")
        if isinstance(code, str) and code.endswith("_test_failed"):
            labels.append("test-failure")
    return sorted(set(labels))


def _bug_report_body(
    *,
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
    verdict: dict[str, Any],
    findings: list[dict[str, Any]],
    next_actions: list[str],
    source_files: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> str:
    job_id = report.get("job_id") or "unknown-job"
    lines = [
        "## Summary",
        "",
        _redact(str(verdict.get("summary") or "No summary available.")),
        "",
        "## Job",
        "",
        f"- job_id: `{job_id}`",
        f"- state: `{report.get('state')}`",
        f"- success: `{report.get('success')}`",
        f"- dry_run: `{report.get('dry_run')}`",
    ]
    metadata = report.get("build_metadata") or {}
    if metadata:
        lines.extend(
            [
                f"- profile: `{metadata.get('profile')}`",
                f"- make_target: `{metadata.get('make_target')}`",
                f"- git_describe: `{metadata.get('git_describe')}`",
            ]
        )
    artifact = report.get("artifact") or {}
    if artifact:
        lines.extend(
            [
                "",
                "## Artifact",
                "",
                f"- filename: `{artifact.get('filename')}`",
                f"- size_bytes: `{artifact.get('size_bytes')}`",
                f"- sha256: `{artifact.get('sha256')}`",
            ]
        )
    if build_summary:
        lines.extend(
            [
                "",
                "## Build",
                "",
                f"- classification: `{build_summary.get('classification')}`",
                f"- failed_package: `{build_summary.get('failed_package')}`",
                f"- failed_step: `{build_summary.get('failed_step')}`",
            ]
        )
    lines.extend(_bug_report_list("Findings", [str(item.get("summary")) for item in findings]))
    lines.extend(_bug_report_list("Suggested Next Actions", next_actions))
    build_evidence = evidence.get("build_summary") if isinstance(evidence, dict) else None
    if build_evidence:
        lines.extend(["", "## Evidence", ""])
        for item in build_evidence[:10]:
            location = f"{item.get('file')}:{item.get('line')}"
            lines.append(f"- `{location}` {_redact(str(item.get('text')))}")
    if source_files:
        lines.extend(["", "## Source Files", ""])
        for source in source_files:
            lines.append(
                f"- `{source.get('path')}` sha256=`{source.get('sha256')}` "
                f"bytes=`{source.get('bytes')}`"
            )
    lines.extend(
        [
            "",
            "## Guardrail",
            "",
            "This draft is advisory. Do not run destructive flash, sysupgrade, "
            "bootloader changes, or cleanup commands based only on this draft.",
        ]
    )
    return "\n".join(lines)


def _bug_report_list(title: str, values: list[str]) -> list[str]:
    compact = [_redact(value) for value in values if value]
    if not compact:
        return []
    lines = ["", f"## {title}", ""]
    lines.extend(f"- {value}" for value in compact)
    return lines


def _result_counts(results: object) -> dict[str, int]:
    if not isinstance(results, list):
        return {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    skipped = sum(1 for item in results if isinstance(item, dict) and item.get("skipped"))
    passed = sum(1 for item in results if isinstance(item, dict) and item.get("passed"))
    failed = len(results) - passed - skipped
    return {"total": len(results), "passed": passed, "failed": failed, "skipped": skipped}


def _build_evidence(run_dir: Path, build_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not build_summary:
        return []
    evidence = build_summary.get("evidence")
    if not isinstance(evidence, list):
        return []
    return _find_line_refs(run_dir / "build.log", [str(item) for item in evidence])


def _find_line_refs(path: Path, needles: list[str]) -> list[dict[str, Any]]:
    if not needles or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    refs: list[dict[str, Any]] = []
    used: set[int] = set()
    for needle in needles:
        for index, line in enumerate(lines, start=1):
            if index in used:
                continue
            if needle == line:
                refs.append({"file": path.name, "line": index, "text": _redact(line)})
                used.add(index)
                break
    return refs


def _tail_evidence(path: Path, max_lines: int) -> list[dict[str, Any]]:
    if max_lines <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [{"file": path.name, "line": None, "text": f"unreadable: {exc}"}]
    start = max(0, len(lines) - max_lines)
    return [
        {"file": path.name, "line": start + offset + 1, "text": _redact(line)}
        for offset, line in enumerate(lines[start:])
    ]


def _source_file_records(run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in _SOURCE_FILES:
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        records.append(
            {
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return records


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _job_result(report: dict[str, Any]) -> str | None:
    if report.get("dry_run"):
        return "dry_run"
    if report.get("success") is True:
        return "success"
    if report.get("success") is False:
        return "failed"
    return None


def _ui_severity(report: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    if any(finding.get("severity") == "error" for finding in findings):
        return "error"
    if any(finding.get("severity") == "warning" for finding in findings):
        return "warning"
    if report.get("dry_run"):
        return "info"
    return "success" if report.get("success") is True else "unknown"


def _ui_badges(
    report: dict[str, Any],
    build_summary: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> list[str]:
    badges: list[str] = []
    state = report.get("state")
    if state:
        badges.append(str(state))
    if report.get("dry_run"):
        badges.append("dry-run")
    if build_summary and build_summary.get("classification"):
        badges.append(f"build:{build_summary['classification']}")
    if any(finding.get("code") == "dut_boot_failure" for finding in findings):
        badges.append("dut:boot-failure")
    return badges


def _redact(value: str) -> str:
    redacted = value
    for pattern, replacement in _SECRET_REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
