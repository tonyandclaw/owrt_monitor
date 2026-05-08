from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from owrt_monitor import __version__
from owrt_monitor.cancel import CancelToken
from owrt_monitor.config import ConfigError, load_config
from owrt_monitor.inspect import JobInspection, diff_pairs, inspect_job
from owrt_monitor.metrics import aggregate_metrics
from owrt_monitor.retention import apply_prune, format_bytes, plan_prune
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


@app.command("dry-run")
def dry_run(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
) -> None:
    """Validate config and write a planned job report without touching Docker or a DUT."""
    _run_build_workflow(config, dry_run=True, allow_flash=False, profile=profile)


@app.command("build")
def build(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
) -> None:
    """Run the Docker build and export the selected firmware artifact."""
    _run_build_workflow(config, dry_run=dry_run_mode, allow_flash=False, profile=profile)


@app.command("run")
def run(
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
    allow_flash: bool = typer.Option(
        False,
        "--allow-flash",
        help="After build/export, transfer firmware to the DUT and run the upgrade command.",
    ),
) -> None:
    """Build/export firmware, and optionally flash the configured DUT."""
    _run_build_workflow(config, dry_run=dry_run_mode, allow_flash=allow_flash, profile=profile)


@app.command("flash")
def flash(
    artifact: Path = ARTIFACT_OPTION,
    config: Path = CONFIG_OPTION,
    profile: str | None = PROFILE_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
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
) -> None:
    """Run configured smoke tests over the DUT serial console."""
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
    if report.test_results:
        passed = sum(1 for result in report.test_results if result["passed"])
        console.print(f"Smoke tests: [bold]{passed}/{len(report.test_results)} passed[/bold]")


@app.command("status")
def status(
    config: Path = CONFIG_OPTION,
    limit: int = LIMIT_OPTION,
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

    table = Table(title=f"Recent jobs ({db_path})")
    table.add_column("Job ID")
    table.add_column("State")
    table.add_column("Result")
    table.add_column("PID")
    table.add_column("Alive")
    table.add_column("Started")
    table.add_column("Run Dir")
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "DRY_RUN"}
    for row in rows:
        pid = row.get("pid")
        if row["state"] in terminal or pid is None:
            alive = "—"
        else:
            alive_state = _is_pid_alive(int(pid))
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
    for key in ("boot_duration_sec", "smoke_duration_sec"):
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
        passed = sum(1 for r in insp.test_results if r.get("passed"))
        total = len(insp.test_results)
        console.print(f"Smoke tests: [bold]{passed}/{total} passed[/bold]")


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
    if report.test_results:
        passed = sum(1 for result in report.test_results if result["passed"])
        console.print(f"Smoke tests: [bold]{passed}/{len(report.test_results)} passed[/bold]")


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
    if report.test_results:
        passed = sum(1 for result in report.test_results if result["passed"])
        console.print(f"Smoke tests: [bold]{passed}/{len(report.test_results)} passed[/bold]")


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
    if report.test_results:
        passed = sum(1 for result in report.test_results if result["passed"])
        console.print(f"Smoke tests: [bold]{passed}/{len(report.test_results)} passed[/bold]")


def main() -> None:
    app()
