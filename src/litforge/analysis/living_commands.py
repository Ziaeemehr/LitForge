"""CLI for immutable snapshots, refresh, explicit scheduling and reproducibility."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

from litforge.analysis import living, repro, snapshots, watch
from litforge.analysis.claim_commands import _required
from litforge.analysis.commands import Approve, Preview, Profile, Project, _context, check_plan
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.matrix import extract_matrix, plan_calls
from litforge.llm.budget import BudgetExceeded
from litforge.llm.router import RoutingError

Md = Annotated[Path | None, typer.Option("--md")]
watch_app = typer.Typer(help="Explicitly write or remove a living-review schedule.", no_args_is_help=True)


def snapshot(ctx: typer.Context, project: Project = None,
             label: Annotated[str, typer.Option("--label")] = "") -> None:
    """Take an immutable snapshot of the current project."""
    with _context(ctx, project) as (_, conn, pid):
        typer.echo(snapshots.take_snapshot(conn, _required(pid), label))


def list_snapshots(ctx: typer.Context, project: Project = None) -> None:
    """List snapshots, newest first."""
    with _context(ctx, project) as (_, conn, pid):
        for item in snapshots.list_snapshots(conn, _required(pid)):
            typer.echo(f"{item.id}  {item.created_at}  {item.label}  {item.content_hash}")


def delta(ctx: typer.Context, old: str, new: str, md: Md = None) -> None:
    """Compare two snapshots and optionally write Markdown."""
    with _context(ctx) as (_, conn, _):
        change = snapshots.diff_snapshots(snapshots.get_snapshot(conn, old), snapshots.get_snapshot(conn, new))
        _output(living.delta_markdown(change), md)


def _output(content: str, md: Path | None) -> None:
    if md:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(content, encoding="utf-8")
        typer.echo(str(md))
    else:
        typer.echo(content)


def refresh(ctx: typer.Context, project: Project = None,
            no_screen: Annotated[bool, typer.Option("--no-screen")] = False,
            no_extract: Annotated[bool, typer.Option("--no-extract")] = False,
            no_claims: Annotated[bool, typer.Option("--no-claims")] = False,
            notify: Annotated[bool, typer.Option("--notify")] = False) -> None:
    """Refresh stored searches and print evidence-change signals."""
    from litforge.cli import _sources
    with _context(ctx, project) as (st, conn, pid):
        try:
            result = living.refresh_project(conn, ModelGateway(conn, st.config), _sources(st, True), _required(pid),
                                            screen=not no_screen, extract=not no_extract, claims=not no_claims,
                                            progress=typer.echo)
        except (RoutingError, BudgetExceeded) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        for signal in result.signals:
            typer.echo(signal.message)
        if not result.signals:
            typer.echo("No significant signals.")
        typer.echo(f"Refresh {result.status}; report: {result.report_path}")
        if notify and result.signals:
            try:
                watch.notify_signals(result.signals)
            except (OSError, subprocess.SubprocessError):
                # An unavailable desktop notifier must not turn a successful refresh into a CLI failure.
                typer.echo("Desktop notification unavailable.", err=True)


@watch_app.command("install")
def install(ctx: typer.Context, project: Project = None,
            every: Annotated[str, typer.Option("--every")] = "7d") -> None:
    """Write schedule files; print the explicit OS activation command."""
    with _context(ctx, project) as (st, conn, pid):
        row = conn.execute("SELECT name FROM projects WHERE id=?", (_required(pid),)).fetchone()
        config_path = ctx.find_root().params.get("config")
        typer.echo(watch.install_watch(row["name"], every, data_dir=st.paths.root, config_path=config_path))


@watch_app.command("remove")
def remove(ctx: typer.Context, project: Project = None) -> None:
    """Remove schedule files; print the explicit OS deactivation command."""
    with _context(ctx, project) as (_, conn, pid):
        row = conn.execute("SELECT name FROM projects WHERE id=?", (_required(pid),)).fetchone()
        typer.echo(watch.remove_watch(row["name"]))


def reproducibility(ctx: typer.Context, project: Project = None,
                    paper: Annotated[str | None, typer.Option("--paper")] = None, md: Md = None,
                    profile: Profile = None, approve: Approve = False, preview: Preview = False) -> None:
    """Extract the built-in reproducibility matrix and write cited reproduction briefs."""
    with _context(ctx, project) as (st, conn, pid):
        if pid is None:
            if paper is None:
                raise ValueError("choose a project or --paper")
            _output(repro.repro_card(conn, paper).brief, md)
            return
        matrix = repro.ensure_repro_matrix(conn, pid)
        gateway = ModelGateway(conn, st.config)
        selected = [paper] if paper else None
        from litforge.analysis.claims import paper_runs
        parsed = list(paper_runs(conn, pid, selected))
        check_plan(gateway, plan_calls(conn, gateway, matrix.id, parsed, profile), approve, preview)
        extract_matrix(conn, gateway, matrix.id, parsed, profile, approve)
        ids = selected or [r[0] for r in conn.execute(
            "SELECT paper_id FROM project_papers WHERE project_id=? ORDER BY paper_id", (pid,))]
        cards = [repro.repro_card(conn, i, pid) for i in ids]
        _output("\n".join(c.brief for c in cards), md)


def register(app: typer.Typer) -> None:
    app.command("snapshot")(snapshot)
    app.command("snapshots")(list_snapshots)
    app.command("delta")(delta)
    app.command("refresh")(refresh)
    app.command("repro")(reproducibility)
    app.add_typer(watch_app, name="watch")
