"""CLI commands for on-demand figure analysis and the proposal review gate."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer

from litforge.analysis import figures, gaps
from litforge.analysis.claim_commands import _required
from litforge.analysis.commands import Approve, Preview, Profile, Project, _context, check_plan
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.runs import canonical_json

asset_app = typer.Typer(help="Analyze selected table, figure and equation crops.", no_args_is_help=True)
gaps_app = typer.Typer(help="Design and review research proposals from the evidence landscape.",
                       invoke_without_command=True)
Json = Annotated[bool, typer.Option("--json", help="Print JSON instead of text")]
SECTIONS = {"why_it_matters": "Why it matters", "minimal_experiment": "Minimal experiment or analysis",
            "data": "Data", "controls": "Controls", "outcome": "Outcome", "failure_modes": "Failure modes",
            "what_model_does_not_know": "What the model does not know"}


def _print_analysis(result: figures.AssetAnalysis) -> None:
    r = result.result
    typer.echo(f"{result.mode} analysis by {result.model} (image-derived; check against the crop)")
    if result.mode == "table":
        units = r.get("units", {})
        typer.echo("\t".join(f"{c} ({units[c]})" if units.get(c) else c for c in r.get("columns", [])))
        for row in r.get("rows", []):
            typer.echo("\t".join(str(cell) for cell in row))
        for note in r.get("footnotes", []):
            typer.echo(f"  note: {note}")
    elif result.mode == "chart":
        typer.echo("axes: " + ", ".join(f"{k}: {v}" for k, v in r.get("axes", {}).items()))
        for series in r.get("series", []):
            points = " ".join(f"({p[0]}, {p[1]})" for p in series.get("points", []) if len(p) >= 2)
            typer.echo(f"  {series.get('name', '')}: {points}  [digitized estimate]")
    else:
        typer.echo(r.get("latex", ""))
        for sym in r.get("symbols", []):
            typer.echo(f"  {sym['symbol']}: {sym['meaning']} [{sym['evidence']}]")
    typer.echo("fidelity: " + "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in result.fidelity.items()))


def _print_card(card: gaps.GapCard) -> None:
    typer.echo(f"{card.title} · {card.review_status} · card {card.id}")
    for key, heading in SECTIONS.items():
        typer.echo(f"\n{heading}")
        for sentence in card.content.get(key, []):
            refs = " ".join(f"[{e}]" for e in sentence.evidence_ids) or f"[{sentence.label}]"
            typer.echo(f"  - {sentence.text} {refs}")


@asset_app.command("analyze")
def analyze_asset(ctx: typer.Context, asset_id: str,
                  mode: Annotated[Literal["table", "chart", "equation"], typer.Option("--mode")],
                  profile: Profile = None, approve: Approve = False, preview: Preview = False,
                  as_json: Json = False) -> None:
    """Analyze one asset crop with the vision model; values read from images stay labelled as such."""
    with _context(ctx) as (st, conn, _):
        gateway = ModelGateway(conn, st.config)
        plan = figures.plan_asset(conn, gateway, asset_id, mode, profile)
        check_plan(gateway, [plan], approve, preview)
        result = figures.analyze_asset(conn, gateway, st.paths, asset_id, mode, profile, approve)
        if as_json:
            typer.echo(result.model_dump_json(indent=1))
        else:
            _print_analysis(result)


def glossary(ctx: typer.Context, project: Project = None, as_json: Json = False) -> None:
    """Equation symbols grouped by meaning across the project's analyzed equations."""
    with _context(ctx, project) as (_, conn, pid):
        terms = figures.glossary(conn, _required(pid))
        if as_json:
            typer.echo(canonical_json(terms))
            return
        if not terms:
            typer.echo("no analyzed equations with explained symbols")
        for term in terms:
            symbols = ", ".join(sorted({s["symbol"] for s in term["symbols"]}))
            typer.echo(f"{term['normalised_meaning']}: {symbols} ({len(term['symbols'])} uses)")


@gaps_app.callback()
def list_candidates(ctx: typer.Context, project: Project = None, as_json: Json = False) -> None:
    """List gap candidates computed from the claim landscape and matrices."""
    if ctx.invoked_subcommand:
        return
    with _context(ctx, project) as (_, conn, pid):
        found = gaps.detect_gaps(conn, _required(pid))
        if as_json:
            typer.echo(canonical_json([gap.model_dump() for gap in found]))
            return
        if not found:
            typer.echo("no gap candidates")
        for gap in found:
            card = f"; card {gap.card_review_status}" if gap.card_id else ""
            typer.echo(f"{gap.id}  {gap.kind}: {gap.title} [{gap.review_status}{card}; "
                       f"{len(gap.evidence_ids)} evidence]")


@gaps_app.command("design")
def design(ctx: typer.Context, gap_id: str, profile: Profile = None,
           approve: Approve = False, preview: Preview = False, as_json: Json = False) -> None:
    """Draft a research-proposal card for a gap; it must be reviewed before export."""
    with _context(ctx) as (st, conn, _):
        gateway = ModelGateway(conn, st.config)
        check_plan(gateway, gaps.plan_gap(conn, gateway, gap_id, profile), approve, preview)
        card = gaps.design_gap(conn, gateway, gap_id, profile, approve)
        if as_json:
            typer.echo(card.model_dump_json(indent=1))
        else:
            _print_card(card)


@gaps_app.command("review")
def review(ctx: typer.Context, card_id: str, action: Literal["accept", "reject"],
           reason: Annotated[str, typer.Option("--reason")] = "", as_json: Json = False) -> None:
    """Accept or reject a proposal card; only accepted cards can be exported."""
    with _context(ctx) as (_, conn, _):
        card = gaps.review_gap(conn, card_id, action, reason)
        typer.echo(card.model_dump_json(indent=1) if as_json else f"card {card.id}: {card.review_status}")


@gaps_app.command("export")
def export(ctx: typer.Context, card_id: str,
           out: Annotated[Path | None, typer.Option("--out", "-o")] = None) -> None:
    with _context(ctx) as (_, conn, _):
        markdown = gaps.export_gap_card(conn, card_id)
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(markdown, encoding="utf-8")
            typer.echo(str(out))
        else:
            typer.echo(markdown, nl=False)


def register(app: typer.Typer) -> None:
    app.add_typer(asset_app, name="asset")
    app.add_typer(gaps_app, name="gaps")
    app.command("glossary")(glossary)
