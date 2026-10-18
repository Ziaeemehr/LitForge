"""CLI commands for the limited research console and YAML workflows."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Annotated

import typer

from litforge.analysis.commands import Project, _context
from litforge.analysis.gateway import ModelGateway
from litforge.console import planner, workflows
from litforge.console.actions import ActionContext
from litforge.console.planner import PlanValidationError

console_app = typer.Typer(help="Preview, execute and resume research workflows.", no_args_is_help=True)
workflow_app = typer.Typer(help="Run built-in typed workflow templates.", no_args_is_help=True)

if TYPE_CHECKING:
    from litforge.cli import State


Json = Annotated[bool, typer.Option("--json", help="Print the preview and run as JSON")]


def _print_preview(estimate: planner.PlanPreview) -> None:
    typer.echo("Plan preview (nothing has run yet):")
    for step in estimate.steps:
        args = ", ".join(f"{k}={len(v)} items" if isinstance(v, list) and v and isinstance(v[0], dict) else f"{k}={v}"
                         for k, v in step.args.items() if v not in (None, [], ""))
        scope = "" if step.papers_in_scope is None else f" · {step.papers_in_scope} papers"
        calls = (" · model calls and cost known only after earlier steps run" if not step.estimate_known
                 else f" · {step.model_call_count} model calls" if step.model_call_count else "")
        flags = (" · writes" if step.writes else "") + (" · needs cloud approval" if step.requires_approval else "")
        typer.echo(f"  {step.step}. {step.action}({args}){scope}{calls}{flags}")
    cost = "unknown" if estimate.max_cost_usd is None else f"${estimate.max_cost_usd:.4f}"
    typer.echo(f"Total: {estimate.model_call_count} model calls · max cost {cost} · "
               f"about {estimate.time_estimate_seconds:.0f} s")


def _summary(output: object) -> str:
    if not isinstance(output, dict):
        return ""
    for key, label in (("path", "wrote"), ("count", "papers"), ("matrix_id", "matrix"), ("snapshot_id", "snapshot"),
                       ("run_id", "run")):
        if key in output:
            return f"{label} {output[key]}"
    return ""


def _print_run(run: planner.ConsoleRun) -> None:
    for item in run.results:
        typer.echo(f"  {item['step']}. {item['action']}: done {_summary(item['output'])}".rstrip())
    reason = f" ({run.error})" if run.status == "failed" and run.error else ""
    typer.echo(f"Run {run.id}: {run.status}{reason}")
    if run.status in ("failed", "cancelled"):
        typer.echo(f"Resume with: litforge console resume {run.id}")


def _context_for(st: State, conn: sqlite3.Connection, project_id: str | None) -> ActionContext:
    from litforge.cli import _sources

    project = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone() if project_id else None
    return ActionContext(conn, ModelGateway(conn, st.config), project_id,
                         str(project["name"]) if project else None, st.paths, _sources(st, True))


def _run_plan(ctx: typer.Context, value: planner.Plan, project: str | None, yes: bool,
              cloud_approved: bool = False, as_json: bool = False) -> None:
    with _context(ctx, project) as (st, conn, pid):
        context = _context_for(st, conn, pid)
        estimate = planner.preview_plan(value, context, st.config.console.seconds_per_model_call)
        if as_json:
            typer.echo(json.dumps(estimate.model_dump(), ensure_ascii=False, indent=2))
        else:
            _print_preview(estimate)
        blocked = estimate.blocked_reasons
        if blocked:
            typer.echo("Plan is blocked: " + "; ".join(blocked), err=True)
            raise typer.Exit(1)
        if estimate.requires_approval and not cloud_approved:
            cloud_approved = typer.confirm("Separately approve the listed cloud model routes?", default=False)
            if not cloud_approved:
                typer.echo("Cloud model route not approved.")
                return
        approved = yes or typer.confirm("Approve and execute these steps?", default=False)
        if not approved:
            typer.echo("Execution not approved.")
            return
        run_id = planner.create_run(conn, pid, value)
        srcs = context.sources if any(step.action == "refresh" for step in value.steps) else None
        run = planner.execute_run(conn, context.gateway, st.paths, pid, run_id, approved=True, sources=srcs,
                                  approved_cloud=cloud_approved, progress=typer.echo)
        if as_json:
            typer.echo(run.model_dump_json(indent=2))
        else:
            _print_run(run)
        if run.status != "done":
            raise typer.Exit(1)


def do_request(ctx: typer.Context, request: str, project: Project = None,
               yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
               approve_cloud: Annotated[bool, typer.Option("--approve-cloud",
                                                           help="Separately approve cloud model routes")] = False,
               as_json: Json = False) -> None:
    """Plan, preview and execute a request using the closed action registry."""
    try:
        with _context(ctx, project) as (st, conn, pid):
            gateway = ModelGateway(conn, st.config)
            planner_call = planner.preview_planner_call(gateway, request)
            cloud_approved = approve_cloud
            if planner_call.blocked_reason:
                raise ValueError(f"Planner route is blocked: {planner_call.blocked_reason}")
            if planner_call.requires_approval and not cloud_approved:
                cloud_approved = typer.confirm(
                    f"Separately approve sending the request and action schema to "
                    f"{planner_call.provider}/{planner_call.model}?", default=False)
            if planner_call.requires_approval and not cloud_approved:
                typer.echo("Cloud planner route not approved.")
                return
            value = planner.plan(conn, gateway, request, pid, cloud_approved)
        _run_plan(ctx, value, project, yes, cloud_approved, as_json)
    except PlanValidationError as exc:
        typer.echo(f"Plan rejected; validation errors: {exc}", err=True)
        raise typer.Exit(1) from exc


@workflow_app.command("run")
def workflow_run(ctx: typer.Context, name: str,
                 param: Annotated[list[str] | None, typer.Option("--param", help="Workflow input key=value")] = None,
                 project: Project = None,
                 yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
                 approve_cloud: Annotated[bool, typer.Option("--approve-cloud",
                                                             help="Separately approve cloud model routes")] = False,
                 as_json: Json = False) -> None:
    """Load a YAML workflow template into the same preview and execute path."""
    values: dict[str, object] = {}
    for item in param or []:
        key, separator, raw = item.partition("=")
        if not separator or not key or key in values:
            raise typer.BadParameter("parameters must be unique key=value pairs")
        import yaml

        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict | list):
            raise typer.BadParameter("workflow parameters must be scalar values")
        values[key] = parsed
    try:
        _run_plan(ctx, workflows.instantiate_workflow(name, values), project, yes, approve_cloud, as_json)
    except PlanValidationError as exc:
        typer.echo(f"Workflow rejected; validation errors: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, KeyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@console_app.command("runs")
def list_console_runs(ctx: typer.Context, project: Project = None) -> None:
    """List checkpointed console runs."""
    with _context(ctx, project) as (_, conn, pid):
        for item in planner.list_runs(conn, pid):
            reason = f"  ({item.error})" if item.status == "failed" and item.error else ""
            typer.echo(f"{item.id}  {item.status}  step {item.current_step}  {item.request[:80]}{reason}")


@console_app.command("resume")
def resume_console_run(ctx: typer.Context, run_id: str,
                       yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
                       approve_cloud: Annotated[bool, typer.Option("--approve-cloud",
                                                                   help="Separately approve cloud model routes")]
                       = False) -> None:
    """Resume a failed or cancelled console run from its next checkpoint."""
    from litforge.cli import _project, _sources, _state

    st = _state(ctx)
    conn = st.db()
    try:
        saved = planner.get_run(conn, run_id)
        row = conn.execute("SELECT name FROM projects WHERE id=?", (saved.project_id,)).fetchone()
        project = str(row["name"]) if row else None
        _, pid = _project(st, conn, project)
        context = _context_for(st, conn, pid)
        preview = planner.preview_plan(saved.plan, context, st.config.console.seconds_per_model_call)
        if preview.requires_approval and not approve_cloud:
            approve_cloud = typer.confirm("Separately approve the listed cloud model routes?", default=False)
            if not approve_cloud:
                return
        if not yes and not typer.confirm(f"Resume run {run_id} at step {saved.current_step}?", default=False):
            return
        sources = _sources(st, True) if any(s.action == "refresh" for s in saved.plan.steps) else None
        result = planner.execute_run(conn, context.gateway, st.paths, pid, run_id, approved=True,
                                     approved_cloud=approve_cloud, sources=sources, progress=typer.echo)
        typer.echo(result.model_dump_json(indent=2))
        if result.status != "done":
            raise typer.Exit(1)
    except (KeyError, ValueError, PermissionError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()


@console_app.command("cancel")
def cancel_console_run(ctx: typer.Context, run_id: str) -> None:
    """Request cancellation between steps of a running console execution."""
    with _context(ctx) as (_, conn, _):
        typer.echo(planner.request_cancel(conn, run_id).model_dump_json(indent=2))


def register(app: typer.Typer) -> None:
    app.add_typer(console_app, name="console")
    app.add_typer(workflow_app, name="workflow")
    app.command("do")(do_request)
