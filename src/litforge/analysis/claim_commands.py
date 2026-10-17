"""CLI adapters for claims, full-text screening and screening evaluation."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer

from litforge.analysis import claim_graph, claims
from litforge.analysis.commands import Approve, Json, Preview, Profile, Project, Reason, _context, check_plan
from litforge.analysis.evaluation import evaluate_screening, load_benchmark
from litforge.analysis.gateway import ModelGateway
from litforge.screening.active import review_queue
from litforge.screening.fulltext import plan_calls, screen_fulltext

claims_app = typer.Typer(help="Extract, connect and review cited claims.", no_args_is_help=True)


def _required(pid: str | None) -> str:
    if pid is None:
        raise ValueError("choose a project with -p or activate one")
    return pid


@claims_app.command("extract")
def extract(ctx: typer.Context, paper: Annotated[list[str] | None, typer.Option("--paper")] = None,
            project: Project = None, profile: Profile = None, approve: Approve = False,
            preview: Preview = False, force: Annotated[bool, typer.Option("--force")] = False) -> None:
    with _context(ctx, project) as (st, conn, pid):
        gateway = ModelGateway(conn, st.config)
        project_id = _required(pid)
        check_plan(gateway, claims.plan_calls(conn, gateway, project_id, paper, profile, force), approve, preview)
        result = claims.extract_claims(conn, gateway, project_id, paper, profile, approve, force)
        typer.echo(f"{len(result.claims)} claims; {result.uncited_dropped} uncited dropped; {result.cached} cached; "
                   f"{result.no_text} no_text; run {result.run_id}" +
                   ("; stopped at budget" if result.budget_stopped else ""))


@claims_app.command("cluster")
def cluster(ctx: typer.Context, project: Project = None,
            threshold: Annotated[float, typer.Option("--threshold")] = .55) -> None:
    with _context(ctx, project) as (_, conn, pid):
        result = claim_graph.cluster_claims(conn, _required(pid), threshold)
        typer.echo(f"{len(result)} clusters")


@claims_app.command("relate")
def relate(ctx: typer.Context, project: Project = None,
           max_pairs: Annotated[int, typer.Option("--max-pairs")] = 200, profile: Profile = None,
           approve: Approve = False, preview: Preview = False) -> None:
    with _context(ctx, project) as (st, conn, pid):
        gateway = ModelGateway(conn, st.config)
        project_id = _required(pid)
        check_plan(gateway, claim_graph.plan_calls(conn, gateway, project_id, max_pairs, profile), approve, preview)
        result = claim_graph.classify_relations(conn, gateway, project_id, max_pairs, profile, approve)
        typer.echo(f"{len(result.relations)} relations; run {result.run_id}" +
                   ("; stopped at budget" if result.budget_stopped else ""))


@claims_app.command("show")
def show(ctx: typer.Context, project: Project = None,
         cluster: Annotated[str | None, typer.Option("--cluster")] = None, as_json: Json = False) -> None:
    with _context(ctx, project) as (_, conn, pid):
        from litforge.analysis.runs import canonical_json
        from litforge.ui.api import lens_claims
        result = lens_claims(conn, _required(pid), cluster)
        if as_json:
            typer.echo(canonical_json(result))
            return
        for c in result["claims"]:
            typer.echo(f"{c['id']} [{c['status']}; {c['paper_id']}] {c['text']}")
        for r in result["relations"]:
            typer.echo(f"{r['source_claim_id']} → {r['target_claim_id']}: {r['relation']}: {r['explanation']}")
            for cause in r["causes"]:
                typer.echo(f"  Hypothesis ({cause['factor']}): {cause['detail']}" +
                           (" [unsupported hypothesis]" if cause["unsupported_hypothesis"] else ""))


@claims_app.command("landscape")
def landscape(ctx: typer.Context, project: Project = None, as_json: Json = False) -> None:
    with _context(ctx, project) as (_, conn, pid):
        from litforge.analysis.runs import canonical_json
        project_id = _required(pid)
        typer.echo(canonical_json([c.model_dump() for c in claim_graph.claim_landscape(conn, project_id)])
                   if as_json else claim_graph.landscape_markdown(conn, project_id))


@claims_app.command("review")
def review(ctx: typer.Context, action: Literal["accept", "reject", "edit"], id: str,
           value: Annotated[str | None, typer.Argument()] = None,
           text: Annotated[str | None, typer.Option("--text")] = None, reason: Reason = "") -> None:
    with _context(ctx) as (_, conn, _):
        typer.echo(claims.review_claim(conn, id, action, text if text is not None else value, reason).model_dump_json())


def queue(ctx: typer.Context, project: Project = None, n: Annotated[int, typer.Option("-n")] = 20,
          as_json: Json = False) -> None:
    with _context(ctx, project) as (_, conn, pid):
        from litforge.analysis.runs import canonical_json
        items = review_queue(conn, _required(pid), n)
        if as_json:
            typer.echo(canonical_json([i.model_dump() for i in items]))
        else:
            for i in items:
                prediction = f"; nearby marks {i.predicted_relevant:.0%}" if i.predicted_relevant is not None else ""
                typer.echo(f"{i.info:.3f} {i.paper_id} {i.title}: {i.stage} {i.model_decision or 'not screened'}"
                           f"{prediction}; {i.reason}")


def fulltext(ctx: typer.Context, project: str | None, top: int | None, profile: str | None,
             approved: bool, preview: bool, as_json: bool) -> None:
    with _context(ctx, project) as (st, conn, pid):
        gateway = ModelGateway(conn, st.config)
        project_id = _required(pid)
        check_plan(gateway, plan_calls(conn, gateway, project_id, top, profile), approved, preview)
        result = screen_fulltext(conn, gateway, project_id, top, profile, approved)
        typer.echo(result.model_dump_json(indent=1) if as_json else
                   f"S2: {result.screened} screened; {result.cached} cached; {len(result.needs_pdf)} need PDF; "
                   f"{result.counts}" + ("; stopped at budget" if result.budget_stopped else ""))


def eval_screening(ctx: typer.Context, benchmark: Path, project: Project = None, as_json: Json = False) -> None:
    with _context(ctx, project) as (st, conn, pid):
        result = evaluate_screening(conn, load_benchmark(benchmark), _required(pid),
                                    st.config.screening.max_false_negative_rate)
        if as_json:
            typer.echo(result.model_dump_json(indent=1))
        else:
            for stage, metrics in result.stages.items():
                typer.echo(f"{stage}: false-negative rate {metrics.false_negative_rate:.3f}; "
                           f"exclusion rate {metrics.exclusion_rate:.3f}; uncertain rate {metrics.uncertain_rate:.3f}; "
                           f"shortlist recall {metrics.shortlist_recall:.3f} "
                           f"({metrics.screened}/{metrics.papers} screened)")
        if not result.passed:
            raise typer.Exit(1)


def register(app: typer.Typer, eval_app: typer.Typer) -> None:
    app.add_typer(claims_app, name="claims")
    app.command("queue")(queue)
    eval_app.command("screening")(eval_screening)
