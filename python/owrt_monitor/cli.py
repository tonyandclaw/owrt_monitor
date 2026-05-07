from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from owrt_monitor.config import ConfigError, load_config
from owrt_monitor.storage import JobStore
from owrt_monitor.workflow import BuildWorkflow, WorkflowError

app = typer.Typer(
    help="Coordinate OpenWrt build artifacts and, in later milestones, DUT upgrade/test flows.",
    no_args_is_help=True,
)
console = Console()
DEFAULT_CONFIG = Path("configs/example.yaml")
CONFIG_OPTION = typer.Option(DEFAULT_CONFIG, "--config", "-c")
DRY_RUN_OPTION = typer.Option(False, "--dry-run", help="Plan the workflow without side effects.")
LIMIT_OPTION = typer.Option(20, "--limit", min=1, max=100)


@app.command("validate")
def validate_config(
    config: Path = CONFIG_OPTION,
) -> None:
    """Validate an owrt_monitor YAML config."""
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Config OK[/green]: {config}")
    console.print(f"Project: [bold]{loaded.project.name}[/bold]")
    console.print(f"Builder container: [bold]{loaded.builder.container}[/bold]")


@app.command("dry-run")
def dry_run(
    config: Path = CONFIG_OPTION,
) -> None:
    """Validate config and write a planned job report without touching Docker or a DUT."""
    _run_build_workflow(config, dry_run=True)


@app.command("build")
def build(
    config: Path = CONFIG_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
) -> None:
    """Run the Docker build and export the selected firmware artifact."""
    _run_build_workflow(config, dry_run=dry_run_mode)


@app.command("run")
def run(
    config: Path = CONFIG_OPTION,
    dry_run_mode: bool = DRY_RUN_OPTION,
    allow_flash: bool = typer.Option(
        False,
        "--allow-flash",
        help="Reserved for the destructive DUT upgrade milestone.",
    ),
) -> None:
    """Run the current MVP workflow: build, select artifact, export, and report."""
    if allow_flash:
        console.print("[red]DUT flash is not implemented in this MVP yet.[/red]")
        raise typer.Exit(2)
    _run_build_workflow(config, dry_run=dry_run_mode)


@app.command("flash")
def flash() -> None:
    """Reserved command for the DUT firmware upgrade milestone."""
    console.print(
        "[yellow]DUT flash support is reserved for the next implementation phase.[/yellow]"
    )
    raise typer.Exit(2)


@app.command("test")
def test_device() -> None:
    """Reserved command for the post-upgrade test milestone."""
    console.print(
        "[yellow]Post-upgrade test support is reserved for the next implementation phase.[/yellow]"
    )
    raise typer.Exit(2)


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
    table.add_column("Started")
    table.add_column("Run Dir")
    for row in rows:
        table.add_row(
            row["id"],
            row["state"],
            row["result"] or "",
            row["started_at"],
            row["artifact_dir"],
        )
    console.print(table)


def _run_build_workflow(config: Path, *, dry_run: bool) -> None:
    try:
        report = BuildWorkflow(config).run(dry_run=dry_run)
    except (ConfigError, WorkflowError) as exc:
        console.print(f"[red]Workflow failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    status_word = "planned" if dry_run else "completed"
    console.print(f"[green]Job {status_word}[/green]: {report.job_id}")
    console.print(f"Run directory: [bold]{report.run_dir}[/bold]")
    console.print(f"Report: [bold]{report.run_dir / 'report.md'}[/bold]")
    if report.artifact is not None:
        console.print(f"Artifact: [bold]{report.artifact.host_path}[/bold]")


def main() -> None:
    app()
