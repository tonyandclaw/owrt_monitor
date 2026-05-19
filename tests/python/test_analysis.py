from __future__ import annotations

import json
from pathlib import Path

from owrt_monitor.analysis import analyze_run_dir, write_analysis_files


def test_analyze_run_dir_redacts_and_preserves_source_refs(tmp_path: Path) -> None:
    run_dir = tmp_path / "job_analyze"
    run_dir.mkdir()
    report = {
        "job_id": "job_analyze",
        "state": "FAILED",
        "success": False,
        "dry_run": False,
        "warnings": ["password=supersecret should not leak"],
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "build.log").write_text(
        "make package/foo\n"
        "make[3]: *** [package/foo/compile] Error 2\n"
        "make: *** [include/owrt2102.mk:163: owrt2102.asus_mt_wifi7_mt7987] Error 2\n",
        encoding="utf-8",
    )
    (run_dir / "runner.output.jsonl").write_text(
        '{"stream":"stderr","line":"token=abc123"}\n',
        encoding="utf-8",
    )

    analysis = analyze_run_dir(run_dir, max_tail_lines=5)

    assert analysis["guardrails"]["advisory_only"] is True
    assert analysis["guardrails"]["dangerous_actions_allowed"] is False
    assert analysis["verdict"]["status"] == "failed"
    assert analysis["build"]["classification"] == "failed_package"
    assert analysis["build"]["failed_package"] == "foo"
    assert analysis["source_files"][0]["path"] == "report.json"
    assert analysis["evidence"]["build_summary"] == [
        {
            "file": "build.log",
            "line": 2,
            "text": "make[3]: *** [package/foo/compile] Error 2",
        }
    ]
    rendered = json.dumps(analysis)
    assert "supersecret" not in rendered
    assert "abc123" not in rendered
    assert "<redacted>" in rendered


def test_analyze_run_dir_includes_bug_report_draft(tmp_path: Path) -> None:
    run_dir = tmp_path / "job_bug"
    run_dir.mkdir()
    report = {
        "job_id": "job_bug",
        "state": "FAILED",
        "success": False,
        "dry_run": False,
        "artifact": {
            "filename": "openwrt.bin",
            "size_bytes": 123,
            "sha256": "abc",
        },
        "build_metadata": {
            "profile": "ap",
            "make_target": "owrt2102.asus_mt_wifi7_mt7987",
            "git_describe": "test-dirty",
        },
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "build.log").write_text(
        "No space left on device\n"
        "make: *** [include/owrt2102.mk:163: owrt2102.asus_mt_wifi7_mt7987] Error 2\n",
        encoding="utf-8",
    )

    analysis = analyze_run_dir(run_dir)
    draft = analysis["bug_report_draft"]

    assert "disk space" in draft["title"]
    assert "build:disk_full" in draft["labels"]
    assert "## Artifact" in draft["body"]
    assert "No space left on device" in draft["body"]
    assert "Do not run destructive flash" in draft["body"]


def test_write_analysis_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "job_write"
    run_dir.mkdir()
    analysis = analyze_run_dir(run_dir)

    json_path, md_path = write_analysis_files(run_dir, analysis)

    assert json_path == run_dir / "analysis.json"
    assert md_path == run_dir / "analysis.md"
    written = json.loads(json_path.read_text(encoding="utf-8"))
    assert written["kind"] == "advisory_analysis"
    markdown = md_path.read_text(encoding="utf-8")
    assert "advisory analysis" in markdown
    assert "Bug Report Draft" in markdown


def test_dry_run_verdict_stays_planned_even_when_report_success_true(tmp_path: Path) -> None:
    run_dir = tmp_path / "job_dry"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "job_id": "job_dry",
                "state": "DRY_RUN",
                "success": True,
                "dry_run": True,
            }
        ),
        encoding="utf-8",
    )

    analysis = analyze_run_dir(run_dir)

    assert analysis["job"]["result"] == "dry_run"
    assert analysis["verdict"]["status"] == "planned"
