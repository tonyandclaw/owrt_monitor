from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from owrt_monitor import __version__
from owrt_monitor.analysis import analyze_run_dir, render_analysis_markdown, write_analysis_files
from owrt_monitor.cancel import CancelToken
from owrt_monitor.config import ConfigError, load_config
from owrt_monitor.dut_serial import discover_serial_ports
from owrt_monitor.dut_workflow import DutWorkflowError, probe_serial_interactive
from owrt_monitor.events import EventLogger
from owrt_monitor.inspect import JobInspection, diff_pairs, inspect_job
from owrt_monitor.metrics import aggregate_metrics
from owrt_monitor.reports import WorkflowReport, mark_report_orphaned
from owrt_monitor.retention import apply_prune, format_bytes, plan_prune
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import (
    BuildWorkflow,
    FlashWorkflow,
    SmokeTestWorkflow,
    WorkflowError,
    cancel_marker_path,
)

app = typer.Typer(
    help="Coordinate OpenWrt build artifacts and, in later milestones, DUT upgrade/test flows.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"owrt-monitor {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print the owrt-monitor version and exit.",
    ),
) -> None:
    """Top-level CLI options. Subcommands listed below."""
console = Console()
DEFAULT_CONFIG = Path("configs/example.yaml")
CONFIG_OPTION = typer.Option(DEFAULT_CONFIG, "--config", "-c")
DRY_RUN_OPTION = typer.Option(False, "--dry-run", help="Plan the workflow without side effects.")
LIMIT_OPTION = typer.Option(20, "--limit", min=1, max=100)
ARTIFACT_OPTION = typer.Option(..., "--artifact", "-a", help="Host firmware image to flash.")
PROFILE_OPTION = typer.Option(
    None,
    "--profile",
    "-p",
    help="Apply the named profile from the config's `profiles:` block before running.",
)
DAEMON_URL_OPTION = typer.Option(
    None,
    "--daemon-url",
    help="Submit the job to owrtd instead of running the workflow process directly.",
)


@app.command("validate")
def validate_config(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
) -> None:
    """Validate an owrt_monitor YAML config."""
    try:
        loaded = load_config(config)
        if profile is not None:
            loaded = loaded.with_profile(profile)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Config OK[/green]: {config}")
    if profile is not None:
        console.print(f"Profile: [bold]{profile}[/bold]")
    console.print(f"Project: [bold]{loaded.project.name}[/bold]")
    console.print(f"Builder container: [bold]{loaded.builder.container}[/bold]")
    if loaded.profiles:
        console.print(f"Available profiles: [bold]{', '.join(sorted(loaded.profiles))}[/bold]")


@app.command("lab-check")
def lab_check(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
) -> None:
    """Check local lab readiness without building or touching a DUT."""
    try:
        loaded = load_config(config)
        if profile is not None:
            loaded = loaded.with_profile(profile)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc

    checks = _lab_readiness_checks(loaded, config.resolve())
    table = Table(title=f"Lab readiness ({config})")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for label, ok, detail in checks:
        table.add_row(label, "[green]ok[/green]" if ok else "[red]fail[/red]", detail)
    console.print(table)
    if not all(ok for _, ok, _ in checks):
        raise typer.Exit(1)


@app.command("dry-run")
def dry_run(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    daemon_url: str | None = DAEMON_URL_OPTION,
) -> None:
    """Validate config and write a planned job report without touching Docker or a DUT."""
    if daemon_url is not None:
        _submit_daemon_job(
            "build",
            config=config,
            profile=profile,
            dry_run=True,
            allow_flash=False,
            artifact=None,
            daemon_url=daemon_url,
        )
        return
    _run_build_workflow(config, dry_run=True, allow_flash=False, profile=profile)


@app.command("build")
def build(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
    daemon_url: str | None = DAEMON_URL_OPTION,
) -> None:
    """Run the Docker build and export the selected firmware artifact."""
    if daemon_url is not None:
        _submit_daemon_job(
            "build",
            config=config,
            profile=profile,
            dry_run=dry_run_mode,
            allow_flash=False,
            artifact=None,
            daemon_url=daemon_url,
        )
        return
    _run_build_workflow(config, dry_run=dry_run_mode, allow_flash=False, profile=profile)


@app.command("run")
def run(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
    daemon_url: str | None = DAEMON_URL_OPTION,
    allow_flash: bool = typer.Option(
        False,
        "--allow-flash",
        help="After build/export, transfer firmware to the DUT and run the upgrade command.",
    ),
) -> None:
    """Build/export firmware, and optionally flash the configured DUT."""
    if daemon_url is not None:
        _submit_daemon_job(
            "run",
            config=config,
            profile=profile,
            dry_run=dry_run_mode,
            allow_flash=allow_flash,
            artifact=None,
            daemon_url=daemon_url,
        )
        return
    _run_build_workflow(config, dry_run=dry_run_mode, allow_flash=allow_flash, profile=profile)


@app.command("flash")
def flash(
    artifact: Path = ARTIFACT_OPTION,
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
    daemon_url: str | None = DAEMON_URL_OPTION,
    allow_flash: bool = typer.Option(
        False,
        "--allow-flash",
        help="Permit the destructive DUT upgrade command.",
    ),
) -> None:
    """Transfer an existing firmware image to the DUT and run the configured upgrade flow."""
    if not dry_run_mode and not allow_flash:
        console.print("[red]Refusing to flash without --allow-flash.[/red]")
        raise typer.Exit(2)
    if daemon_url is not None:
        _submit_daemon_job(
            "flash",
            config=config,
            profile=profile,
            dry_run=dry_run_mode,
            allow_flash=allow_flash,
            artifact=artifact,
            daemon_url=daemon_url,
        )
        return
    _run_flash_workflow(
        config,
        artifact=artifact,
        dry_run=dry_run_mode,
        allow_flash=allow_flash,
        profile=profile,
    )


@app.command("test")
def test_device(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
    daemon_url: str | None = DAEMON_URL_OPTION,
) -> None:
    """Run configured DUT tests without building or flashing."""
    if daemon_url is not None:
        _submit_daemon_job(
            "test",
            config=config,
            profile=profile,
            dry_run=dry_run_mode,
            allow_flash=False,
            artifact=None,
            daemon_url=daemon_url,
        )
        return
    _run_smoke_test_workflow(config, dry_run=dry_run_mode, profile=profile)


@app.command("resume")
def resume(
    job_id: str = typer.Argument(..., help="Job ID to resume."),
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
    allow_flash: bool = typer.Option(
        False,
        "--allow-flash",
        help="Permit the destructive DUT upgrade command when resuming.",
    ),
) -> None:
    """Resume a previous job from BUILD_SUCCEEDED, ARTIFACT_SELECTED, or ARTIFACT_EXPORTED."""
    try:
        report = BuildWorkflow(config, profile=profile).resume(
            job_id,
            dry_run=dry_run_mode,
            allow_flash=allow_flash,
        )
    except (ConfigError, WorkflowError) as exc:
        console.print(f"[red]Resume failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    status_word = "planned" if dry_run_mode else "completed"
    console.print(f"[green]Resume {status_word}[/green]: {report.job_id}")
    console.print(f"Run directory: [bold]{report.run_dir}[/bold]")
    console.print(f"Report: [bold]{report.run_dir / 'report.md'}[/bold]")
    _print_post_upgrade_summary(report)


@app.command("status")
def status(
    config: Path = CONFIG_OPTION,
    limit: int = LIMIT_OPTION,
    mark_orphans: bool = typer.Option(
        False,
        "--mark-orphans",
        help="Mark non-terminal jobs with dead PIDs as FAILED/orphan and release their locks.",
    ),
) -> None:
    """Show recent jobs from the configured SQLite state database."""
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc

    db_path = loaded.state_db_path(config.resolve())
    store = JobStore(db_path)
    rows = store.recent_jobs(limit=limit)
    marked_orphans: set[str] = set()

    table = Table(title=f"Recent jobs ({db_path})")
    table.add_column("Job ID")
    table.add_column("State")
    table.add_column("Result")
    table.add_column("PID")
    table.add_column("Alive")
    table.add_column("Started")
    table.add_column("Run Dir")
    for row in rows:
        pid = row.get("pid")
        if row["state"] in _TERMINAL_STATES or pid is None:
            alive = "—"
        else:
            alive_state = _is_pid_alive(int(pid))
            if mark_orphans and _is_orphaned_row(row, alive_state):
                _mark_orphaned_job(store, row)
                marked_orphans.add(row["id"])
                row["state"] = JobState.FAILED.value
                row["result"] = "orphan"
                alive_state = False
            alive = {True: "yes", False: "[red]no[/red]", None: "?"}[alive_state]
        table.add_row(
            row["id"],
            row["state"],
            row["result"] or "",
            str(pid or ""),
            alive,
            row["started_at"],
            row["artifact_dir"],
        )
    console.print(table)
    if marked_orphans:
        console.print(
            "[yellow]Marked orphan job(s):[/yellow] "
            + ", ".join(sorted(marked_orphans))
        )


_TERMINAL_STATES = {
    JobState.SUCCEEDED.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
    JobState.DRY_RUN.value,
}


def _is_orphaned_row(row: dict, alive_state: bool | None) -> bool:
    return row["state"] not in _TERMINAL_STATES and alive_state is False


def _mark_orphaned_job(store: JobStore, row: dict) -> None:
    job_id = row["id"]
    run_dir = Path(row["artifact_dir"])
    pid = row.get("pid")
    warning = (
        "job marked orphaned by `owrt-monitor status --mark-orphans`; "
        f"recorded PID {pid} is not running"
    )
    store.update_job(job_id=job_id, state=JobState.FAILED.value, result="orphan")
    released = store.release_locks_for_job(owner_job_id=job_id)
    EventLogger(store=store, job_id=job_id, path=run_dir / "events.jsonl").emit(
        level="WARN",
        component="workflow",
        event="job_orphaned",
        message=warning,
        fields={"run_dir": str(run_dir), "pid": pid, **released},
    )
    mark_report_orphaned(run_dir, job_id=job_id, warning=warning)


def _is_pid_alive(pid: int) -> bool | None:
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


@app.command("inspect")
def inspect(
    job_id: str = typer.Argument(..., help="Job ID to inspect."),
    config: Path = CONFIG_OPTION,
    diff: str | None = typer.Option(
        None,
        "--diff",
        help="Compare against another job ID side-by-side.",
    ),
) -> None:
    """Show a structured summary of one job, or diff two jobs side-by-side."""
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc
    store = JobStore(loaded.state_db_path(config.resolve()))
    primary = inspect_job(store, job_id)
    if primary is None:
        console.print(f"[red]No job with id {job_id!r}.[/red]")
        raise typer.Exit(1)

    if diff is None:
        _print_inspection(primary)
        return

    other = inspect_job(store, diff)
    if other is None:
        console.print(f"[red]No job with id {diff!r}.[/red]")
        raise typer.Exit(1)
    _print_diff(primary, other)


@app.command("analyze")
def analyze(
    target: str = typer.Argument(..., help="Job ID or run directory to analyze."),
    config: Path = CONFIG_OPTION,
    output_format: str = typer.Option(
        "summary",
        "--format",
        help="Output format: summary, markdown, or json.",
    ),
    write: bool = typer.Option(
        True,
        "--write/--no-write",
        help="Persist analysis.json and analysis.md in the run directory.",
    ),
    tail_lines: int = typer.Option(
        40,
        "--tail-lines",
        min=0,
        max=500,
        help="Number of tail lines to include from each log source.",
    ),
) -> None:
    """Generate advisory, redacted analysis for an existing job/run directory."""
    try:
        run_dir = _resolve_analysis_run_dir(target, config)
    except ConfigError as exc:
        console.print(f"[red]Analysis failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    analysis = analyze_run_dir(run_dir, max_tail_lines=tail_lines)
    json_path: Path | None = None
    md_path: Path | None = None
    if write:
        json_path, md_path = write_analysis_files(run_dir, analysis)

    if output_format == "json":
        console.print_json(json.dumps(analysis, sort_keys=True))
        return
    if output_format == "markdown":
        if md_path is None:
            console.print(render_analysis_markdown(analysis))
            return
        console.print(md_path.read_text(encoding="utf-8"))
        return
    if output_format != "summary":
        console.print("[red]Invalid --format:[/red] expected summary, markdown, or json")
        raise typer.Exit(2)

    _print_analysis_summary(analysis, json_path=json_path, md_path=md_path)


def _resolve_analysis_run_dir(target: str, config: Path) -> Path:
    candidate = Path(target).expanduser()
    if candidate.is_dir():
        return candidate.resolve()

    loaded = load_config(config)
    store = JobStore(loaded.state_db_path(config.resolve()))
    record = store.get_job(target)
    if record is None:
        raise ConfigError(f"no job with id {target!r}; pass a run directory instead")
    return Path(record["artifact_dir"]).resolve()


def _print_analysis_summary(
    analysis: dict[str, object],
    *,
    json_path: Path | None,
    md_path: Path | None,
) -> None:
    job = analysis.get("job") or {}
    verdict = analysis.get("verdict") or {}
    ui_summary = analysis.get("ui_summary") or {}
    table = Table(title=f"Analysis {job.get('job_id', '')}")
    table.add_column("Field")
    table.add_column("Value")
    rows = [
        ("verdict", str(verdict.get("status", "unknown"))),
        ("summary", str(verdict.get("summary", ""))),
        ("severity", str(ui_summary.get("severity", "unknown"))),
        ("run_dir", str(job.get("run_dir", ""))),
    ]
    if json_path is not None:
        rows.append(("analysis.json", str(json_path)))
    if md_path is not None:
        rows.append(("analysis.md", str(md_path)))
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)

    actions = analysis.get("next_actions") or []
    if actions:
        console.print("[bold]Next actions[/bold]")
        for action in actions:
            console.print(f"- {action}")


def _print_inspection(insp: JobInspection) -> None:
    table = Table(title=f"Job {insp.job_id}")
    table.add_column("Field")
    table.add_column("Value")
    rows = [
        ("state", insp.state),
        ("result", insp.result or "—"),
        ("started_at", insp.started_at),
        ("finished_at", insp.finished_at or "—"),
        ("run_dir", str(insp.run_dir)),
    ]
    artifact = insp.artifact or {}
    if artifact:
        rows.extend(
            [
                ("artifact.filename", str(artifact.get("filename", "—"))),
                ("artifact.size_bytes", str(artifact.get("size_bytes", "—"))),
                ("artifact.sha256", str(artifact.get("sha256", "—"))),
            ]
        )
    metadata = insp.build_metadata or {}
    for key in ("profile", "make_target", "git_commit", "git_describe", "git_dirty"):
        if key in metadata:
            rows.append((f"provenance.{key}", str(metadata[key])))
    summary = insp.build_summary or {}
    if summary:
        rows.append(("build.classification", str(summary.get("classification", "—"))))
        if summary.get("duration_sec") is not None:
            rows.append(("build.duration_sec", f"{float(summary['duration_sec']):.2f}"))
    metrics = insp.metrics or {}
    for key in (
        "boot_duration_sec",
        "flash_duration_sec",
        "test_duration_sec",
        "smoke_duration_sec",
        "script_duration_sec",
        "pytest_duration_sec",
        "ssh_duration_sec",
    ):
        if key in metrics:
            rows.append((f"metrics.{key}", f"{float(metrics[key]):.2f}"))
    dut = insp.dut_status or {}
    release = (dut.get("release") or {}) if dut else {}
    for key in ("kernel", "hostname", "board", "model"):
        if dut.get(key) is not None:
            rows.append((f"dut.{key}", str(dut[key])))
    if release.get("distribution") and release.get("version"):
        rows.append(
            ("dut.release", f"{release['distribution']} {release['version']}")
        )

    for label, value in rows:
        table.add_row(label, value)
    console.print(table)

    if insp.test_results:
        console.print(_inspection_result_line("Smoke tests", insp.test_results))
    if insp.script_results:
        console.print(_inspection_result_line("Custom scripts", insp.script_results))
    if insp.pytest_results:
        console.print(_inspection_result_line("Pytest tests", insp.pytest_results))
    if insp.ssh_results:
        console.print(_inspection_result_line("SSH tests", insp.ssh_results))


def _inspection_result_line(label: str, results: list[dict]) -> str:
    passed = sum(1 for r in results if r.get("passed"))
    skipped = sum(1 for r in results if r.get("skipped"))
    suffix = f", {skipped} skipped" if skipped else ""
    return f"{label}: [bold]{passed}/{len(results)} passed{suffix}[/bold]"


def _print_diff(left: JobInspection, right: JobInspection) -> None:
    table = Table(title=f"Diff {left.job_id} vs {right.job_id}")
    table.add_column("Field")
    table.add_column(left.job_id)
    table.add_column(right.job_id)
    table.add_column("Same?", justify="center")
    for label, lv, rv in diff_pairs(left, right):
        if lv == "—" or rv == "—":
            same = "?"
        elif lv == rv:
            same = "yes"
        else:
            same = "[red]no[/red]"
        table.add_row(label, lv, rv, same)
    console.print(table)


@app.command("prune")
def prune(
    config: Path = CONFIG_OPTION,
    keep_success: int = typer.Option(10, "--keep-success", min=0,
                                     help="Most-recent N successful jobs to keep."),
    keep_failed: int = typer.Option(5, "--keep-failed", min=0,
                                    help="Most-recent N failed jobs to keep."),
    keep_other: int = typer.Option(5, "--keep-other", min=0,
                                   help="Most-recent N jobs of any other result to keep."),
    apply: bool = typer.Option(False, "--apply",
                               help="Actually delete. Without this flag, prune is dry-run."),
    limit: int = typer.Option(1000, "--limit", min=1, max=10000,
                              help="Max recent jobs to consider when planning."),
) -> None:
    """Plan or apply pruning of old job run directories.

    By default this is dry-run: prints a plan of what would be deleted.
    Use `--apply` to actually remove the run_dirs. The SQLite job records are
    never touched, so `owrt-monitor status` and `metrics` still see all jobs.
    """
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc

    store = JobStore(loaded.state_db_path(config.resolve()))
    artifact_root = loaded.artifact_root(config.resolve())
    plan = plan_prune(
        store,
        keep_success=keep_success,
        keep_failed=keep_failed,
        keep_other=keep_other,
        artifact_root=artifact_root,
        limit=limit,
    )

    keep_summary = ", ".join(
        f"{result}={count}" for result, count in sorted(plan.kept_count_by_result.items())
    ) or "(no jobs in DB)"
    console.print(f"Keeping: [bold]{keep_summary}[/bold]")

    if not plan.targets:
        console.print("[green]Nothing to prune.[/green]")
        return

    table = Table(title="Prune targets")
    table.add_column("Job ID")
    table.add_column("Result")
    table.add_column("Started")
    table.add_column("Size", justify="right")
    table.add_column("Run Dir")
    for target in plan.targets:
        table.add_row(
            target.job_id,
            target.result,
            target.started_at,
            format_bytes(target.size_bytes),
            str(target.run_dir),
        )
    console.print(table)
    console.print(
        f"Total reclaimable: [bold]{format_bytes(plan.total_bytes)}[/bold] "
        f"across {len(plan.targets)} run directories."
    )

    if not apply:
        console.print(
            "[yellow]Dry-run.[/yellow] Re-run with --apply to actually delete."
        )
        return

    freed = apply_prune(plan.targets)
    console.print(
        f"[green]Pruned {len(plan.targets)} run dir(s); freed "
        f"{format_bytes(freed)}.[/green]"
    )


@app.command("metrics")
def metrics(
    config: Path = CONFIG_OPTION,
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """Aggregate metrics across the most-recent jobs (success rate + durations)."""
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc

    store = JobStore(loaded.state_db_path(config.resolve()))
    rows = store.recent_metrics(limit=limit)
    summary = aggregate_metrics(rows)

    console.print(
        f"Aggregated over [bold]{summary.total_jobs}[/bold] most-recent job(s)."
    )

    counts_table = Table(title="Result breakdown")
    counts_table.add_column("Result")
    counts_table.add_column("Count")
    for result, count in sorted(summary.counts_by_result.items()):
        counts_table.add_row(result, str(count))
    console.print(counts_table)

    if summary.success_rate is None:
        console.print("Success rate: [yellow]n/a[/yellow] (no terminal success/failed jobs)")
    else:
        pct = summary.success_rate * 100
        colour = "green" if pct >= 90 else ("yellow" if pct >= 60 else "red")
        console.print(f"Success rate: [{colour}]{pct:.1f}%[/{colour}]")

    if not summary.durations:
        console.print("[yellow]No duration metrics recorded yet.[/yellow]")
        return

    duration_table = Table(title="Duration stats (seconds)")
    duration_table.add_column("Metric")
    duration_table.add_column("N", justify="right")
    duration_table.add_column("Mean", justify="right")
    duration_table.add_column("Median", justify="right")
    duration_table.add_column("p90", justify="right")
    duration_table.add_column("Min", justify="right")
    duration_table.add_column("Max", justify="right")
    for name in sorted(summary.durations):
        stats = summary.durations[name]
        duration_table.add_row(
            name,
            str(stats.count),
            f"{stats.mean:.2f}",
            f"{stats.median:.2f}",
            f"{stats.p90:.2f}",
            f"{stats.minimum:.2f}",
            f"{stats.maximum:.2f}",
        )
    console.print(duration_table)


@app.command("cancel")
def cancel(
    job_id: str = typer.Argument(..., help="Job ID to cancel."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Request cancellation of a running job by writing a marker file in its run directory."""
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc

    store = JobStore(loaded.state_db_path(config.resolve()))
    record = store.get_job(job_id)
    if record is None:
        console.print(f"[red]No job with id {job_id!r} found.[/red]")
        raise typer.Exit(1)

    run_dir = Path(record["artifact_dir"])
    token = CancelToken(cancel_marker_path(run_dir))
    token.request()
    console.print(f"[yellow]Cancellation requested[/yellow] for {job_id} (state={record['state']})")
    console.print(f"Marker: [bold]{token.marker_path}[/bold]")
    if record.get("pid"):
        console.print(
            f"Workflow PID: [bold]{record['pid']}[/bold] "
            "(send SIGTERM manually if it is wedged on a non-cancellable read)"
        )


def _lab_readiness_checks(config, config_path: Path) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    builder_ok, builder_detail = _builder_readiness(config.builder.container)
    checks.append(("builder container", builder_ok, builder_detail))
    transcript_path = config.artifact_root(config_path) / "lab-check-serial.log"
    serial_ok, serial_detail = _serial_readiness(
        config.dut.serial,
        config.dut.discovery_patterns,
        config=config,
        transcript_path=transcript_path,
    )
    checks.append(("serial prompt", serial_ok, serial_detail))
    network_ok, network_detail = _network_readiness(config.dut.network, config.upgrade)
    checks.append(("DUT network", network_ok, network_detail))
    transfer_ok, transfer_detail = _transfer_readiness(config.upgrade)
    checks.append(("firmware transfer", transfer_ok, transfer_detail))
    return checks


def _builder_readiness(container: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"cannot inspect {container!r}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or f"{container!r} not found").strip()
    running = result.stdout.strip() == "true"
    if not running:
        return False, f"{container!r} is not running"
    return True, f"{container!r} is running"


def _serial_readiness(
    serial: str | None,
    patterns: list[str],
    *,
    config=None,
    transcript_path: Path | None = None,
) -> tuple[bool, str]:
    if serial:
        if not Path(serial).exists():
            candidates = discover_serial_ports(patterns)
            detail = f"configured serial {serial!r} is missing"
            if candidates:
                detail += "; candidates: " + ", ".join(candidates)
            return False, detail
        return _serial_prompt_readiness(serial, config=config, transcript_path=transcript_path)
    candidates = discover_serial_ports(patterns)
    if len(candidates) == 1:
        return _serial_prompt_readiness(
            candidates[0],
            config=config,
            transcript_path=transcript_path,
        )
    if not candidates:
        return False, "no serial devices matched discovery_patterns"
    return False, "multiple serial candidates; set dut.serial: " + ", ".join(candidates)


def _serial_prompt_readiness(
    port: str,
    *,
    config=None,
    transcript_path: Path | None = None,
) -> tuple[bool, str]:
    if config is None or transcript_path is None:
        return True, port
    try:
        interactive_port = probe_serial_interactive(config, transcript_path)
    except DutWorkflowError as exc:
        return False, f"{exc}; transcript: {transcript_path}"
    return True, f"{interactive_port} prompt matched; transcript: {transcript_path}"


def _transfer_readiness(upgrade) -> tuple[bool, str]:
    if upgrade.transfer in {"tftp", "bootloader_tftp"}:
        host = upgrade.tftp_host or upgrade.http_host
        if not host:
            return False, "upgrade.tftp_host or upgrade.http_host is required"
        root = Path(upgrade.tftp_root)
        if not root.exists():
            return False, f"TFTP root does not exist: {root}"
        if not os.access(root, os.W_OK):
            return False, f"TFTP root is not writable by this user: {root}"
        return True, f"{upgrade.transfer} via {host}, root={root}"
    if upgrade.transfer == "scp":
        host = upgrade.scp_host
        return True, f"scp host={host or '<dut.network.address>'}"
    if upgrade.transfer == "custom":
        return True, "custom transfer command configured"
    return True, f"{upgrade.transfer} transfer"


def _network_readiness(network, upgrade) -> tuple[bool, str]:
    address = network.address
    if not address:
        if upgrade.transfer in {"scp"}:
            return False, "dut.network.address is required for this transfer"
        return True, "no dut.network.address configured; skipping ping"
    if upgrade.transfer == "custom":
        return True, f"{address} configured; custom transfer owns network checks"
    command = _ping_command(address)
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"cannot ping {address}: {exc}"
    if result.returncode == 0:
        return True, f"{address} reachable"
    detail = _compact_process_output(result.stderr or result.stdout or "ping failed")
    return False, f"{address} not reachable: {detail}"


def _ping_command(address: str) -> list[str]:
    if sys.platform == "darwin":
        return ["ping", "-c", "1", "-W", "1000", address]
    return ["ping", "-c", "1", "-W", "1", address]


def _compact_process_output(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "command failed"
    for line in reversed(lines):
        if "packet loss" in line.lower():
            return line
    return lines[-1]


def _daemon_job_payload(
    command: str,
    *,
    config: Path,
    profile: str | None,
    dry_run: bool,
    allow_flash: bool,
    artifact: Path | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "command": command,
        "config": str(config.resolve()),
        "dry_run": dry_run,
        "allow_flash": allow_flash,
        "working_dir": str(Path.cwd()),
    }
    if profile is not None:
        payload["profile"] = profile
    if artifact is not None:
        payload["artifact"] = str(artifact.resolve())
    return payload


def _submit_daemon_job(
    command: str,
    *,
    config: Path,
    profile: str | None,
    dry_run: bool,
    allow_flash: bool,
    artifact: Path | None,
    daemon_url: str,
) -> None:
    payload = _daemon_job_payload(
        command,
        config=config,
        profile=profile,
        dry_run=dry_run,
        allow_flash=allow_flash,
        artifact=artifact,
    )
    url = daemon_url.rstrip("/") + "/v1/jobs"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        console.print(f"[red]owrtd rejected job[/red] ({exc.code}): {body}")
        raise typer.Exit(1) from exc
    except urllib.error.URLError as exc:
        console.print(f"[red]Could not reach owrtd:[/red] {exc.reason}")
        raise typer.Exit(1) from exc

    if status != 202:
        console.print(f"[red]Unexpected owrtd status[/red] {status}: {body}")
        raise typer.Exit(1)
    try:
        accepted = json.loads(body)
    except json.JSONDecodeError as exc:
        console.print(f"[red]owrtd returned invalid JSON:[/red] {body}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Submitted to owrtd[/green]: {accepted['job_id']}")
    console.print(f"PID: [bold]{accepted['pid']}[/bold]")
    console.print(f"Run directory: [bold]{accepted['run_dir']}[/bold]")
    console.print(f"Runner status: [bold]{accepted['run_dir']}/runner.json[/bold]")


def _run_build_workflow(
    config: Path,
    *,
    dry_run: bool,
    allow_flash: bool,
    profile: str | None = None,
) -> None:
    try:
        report = BuildWorkflow(config, profile=profile).run(
            dry_run=dry_run, allow_flash=allow_flash
        )
    except (ConfigError, WorkflowError) as exc:
        console.print(f"[red]Workflow failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    status_word = "planned" if dry_run else "completed"
    console.print(f"[green]Job {status_word}[/green]: {report.job_id}")
    console.print(f"Run directory: [bold]{report.run_dir}[/bold]")
    console.print(f"Report: [bold]{report.run_dir / 'report.md'}[/bold]")
    if report.artifact is not None:
        console.print(f"Artifact: [bold]{report.artifact.host_path}[/bold]")
    _print_post_upgrade_summary(report)


def _run_flash_workflow(
    config: Path,
    *,
    artifact: Path,
    dry_run: bool,
    allow_flash: bool,
    profile: str | None = None,
) -> None:
    try:
        report = FlashWorkflow(config, profile=profile).run(
            artifact_path=artifact,
            dry_run=dry_run,
            allow_flash=allow_flash,
        )
    except (ConfigError, WorkflowError) as exc:
        console.print(f"[red]Workflow failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    status_word = "planned" if dry_run else "completed"
    console.print(f"[green]Flash job {status_word}[/green]: {report.job_id}")
    console.print(f"Run directory: [bold]{report.run_dir}[/bold]")
    console.print(f"Report: [bold]{report.run_dir / 'report.md'}[/bold]")
    _print_post_upgrade_summary(report)


def _run_smoke_test_workflow(
    config: Path,
    *,
    dry_run: bool,
    profile: str | None = None,
) -> None:
    try:
        report = SmokeTestWorkflow(config, profile=profile).run(dry_run=dry_run)
    except (ConfigError, WorkflowError) as exc:
        console.print(f"[red]Workflow failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    status_word = "planned" if dry_run else "completed"
    console.print(f"[green]Smoke test job {status_word}[/green]: {report.job_id}")
    console.print(f"Run directory: [bold]{report.run_dir}[/bold]")
    console.print(f"Report: [bold]{report.run_dir / 'report.md'}[/bold]")
    _print_post_upgrade_summary(report)


def _post_upgrade_summary_lines(report: WorkflowReport) -> list[str]:
    lines: list[str] = []
    for label, results in (
        ("Smoke tests", report.test_results),
        ("Custom scripts", report.script_results),
        ("Pytest tests", report.pytest_results),
        ("SSH tests", report.ssh_results),
    ):
        if not results:
            continue
        passed = sum(1 for result in results if result.get("passed"))
        skipped = sum(1 for result in results if result.get("skipped"))
        suffix = f", {skipped} skipped" if skipped else ""
        lines.append(f"{label}: [bold]{passed}/{len(results)} passed{suffix}[/bold]")
    return lines


def _print_post_upgrade_summary(report: WorkflowReport) -> None:
    for line in _post_upgrade_summary_lines(report):
        console.print(line)


def main() -> None:
    app()
