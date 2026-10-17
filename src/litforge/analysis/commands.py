"""Typer commands for evidence answers, review, matrices and briefs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer
import yaml
from pydantic import ValidationError

from litforge.analysis import ask as answers
from litforge.analysis import audit as audits
from litforge.analysis.brief import write_brief
from litforge.analysis.evaluation import evaluate_answers, evaluate_matrix, load_benchmark
from litforge.analysis.gateway import CallPreview, GatewayError, ModelGateway
from litforge.analysis.matrix import (
    MatrixColumn,
    add_column,
    create_matrix,
    export_matrix,
    extract_matrix,
    list_matrices,
    load_columns,
)
from litforge.analysis.matrix import (
    plan_calls as matrix_plan,
)
from litforge.analysis.review import record_review, review_queue
from litforge.analysis.runs import get_run
from litforge.llm.base import LlmError

if TYPE_CHECKING:
    from litforge.cli import State

Project = Annotated[str | None, typer.Option("--project", "-p")]
Profile = Annotated[str | None, typer.Option("--profile")]
Approve = Annotated[bool, typer.Option("--approve")]
Preview = Annotated[bool, typer.Option("--preview")]
Json = Annotated[bool, typer.Option("--json")]
Reason = Annotated[str, typer.Option("--reason")]
Output = Annotated[Path | None, typer.Option("-o", "--out")]

matrix_app = typer.Typer(help="Create, extract and export study matrices.", no_args_is_help=True)
review_app = typer.Typer(help="Review flagged sentences and cells.", invoke_without_command=True)


@contextmanager
def _context(ctx: typer.Context, project: str | None = None) -> Iterator[tuple[State, sqlite3.Connection, str | None]]:
    from litforge.cli import _project, _state
    st = _state(ctx)
    conn = st.db()
    try:
        _, pid = _project(st, conn, project)
        yield st, conn, pid
    except ValidationError as exc:
        typer.echo("ValidationError: invalid model response", err=True)
        raise typer.Exit(1) from exc
    except (OSError, ValueError, KeyError, sqlite3.IntegrityError, yaml.YAMLError, GatewayError, LlmError) as exc:
        typer.echo(" ".join(str(exc).splitlines()), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()


def check_plan(gateway: ModelGateway, previews: list[CallPreview], approved: bool, preview: bool) -> None:
    blocked = [p.blocked_reason for p in previews if p.blocked_reason]
    requires = any(p.requires_approval for p in previews) and not approved
    if preview or blocked or requires:
        aggregate = {
            "profiles": sorted({p.profile for p in previews}),
            "providers": sorted({p.provider for p in previews}), "models": sorted({p.model for p in previews}),
            "remote": any(p.remote for p in previews), "number_of_calls": len(previews),
            "evidence_ids_count": len({e for p in previews for e in p.evidence_ids}),
            "estimated_input_tokens": sum(p.estimated_input_tokens for p in previews),
            "max_cost_usd": None if any(p.max_cost_usd is None for p in previews) else
                            sum(p.max_cost_usd or 0 for p in previews),
            "blocked_reasons": blocked, "requires_approval": requires,
            "calls": [p.model_dump() for p in previews],
        }
        typer.echo(json.dumps(aggregate, ensure_ascii=False, indent=1))
    if preview:
        raise typer.Exit(0)
    if requires:
        raise typer.Exit(3)
    if blocked:
        raise typer.Exit(1)


def run_ask(ctx: typer.Context, question: str, project: str | None, k: int, profile: str | None,
            approved: bool, preview: bool, audit: bool, audit_profile: str | None,
            sensitive: bool, as_json: bool) -> None:
    with _context(ctx, project) as (st, conn, pid):
        gateway = ModelGateway(conn, st.config)
        plan = answers.plan_calls(conn, gateway, question, pid, k, profile, sensitive)
        if audit and plan:
            # Conservative pre-generation estimate: at most eight cited sentences, each audited once.
            plan += answers.plan_calls(conn, gateway, question, pid, k, audit_profile or profile, sensitive) * 8
        check_plan(gateway, plan, approved, preview)
        answer = answers.ask(conn, gateway, question, pid, k, profile, approved, sensitive)
        if audit:
            audits.audit_answer(conn, gateway, answer.run_id, audit_profile or profile, approved)
            answer = answers.get_answer(conn, answer.run_id)
        if as_json:
            typer.echo(answer.model_dump_json(indent=1))
            return
        ranks = {s.id: rank for rank, s in enumerate(answer.evidence, 1)}
        typer.echo(f"Answer run: {answer.run_id}")
        for sentence in answer.sentences:
            markers = "".join(f"[{ranks[e]}]" for e in sentence.evidence_ids)
            flags = ", ".join(sentence.flags)
            typer.echo(f"{sentence.text}{markers} [{sentence.kind}; audit={sentence.audit_verdict or "not audited"}"
                       + (f"; {flags}" if flags else "") + "]")
        for rank, span in enumerate(answer.evidence, 1):
            quote = " ".join(span.text.split())
            if len(quote) > 300:
                quote = quote[:299].rsplit(" ", 1)[0] + "…"
            typer.echo(f"{rank}. [{span.id}] {span.citation} — {span.section or span.kind}\n   “{quote}”")


def audit(ctx: typer.Context, run_id: str, profile: Profile = None, approve: Approve = False,
          preview: Preview = False) -> None:
    """Audit a stored answer against its cited spans."""
    with _context(ctx) as (st, conn, _):
        gateway = ModelGateway(conn, st.config)
        check_plan(gateway, audits.plan_calls(conn, gateway, run_id, profile), approve, preview)
        result = audits.audit_answer(conn, gateway, run_id, profile, approve)
        for sentence in result:
            typer.echo(f"{sentence.id}: {sentence.audit_verdict} — {sentence.audit_note}")


@review_app.callback()
def review(ctx: typer.Context, project: Project = None, as_json: Json = False) -> None:
    """List open review items, newest first."""
    if ctx.invoked_subcommand:
        return
    with _context(ctx, project) as (_, conn, pid):
        items = review_queue(conn, pid)
        if as_json:
            typer.echo(json.dumps([i.model_dump() for i in items], ensure_ascii=False, indent=1))
        else:
            for item in items:
                context = item.question if item.target_kind == "sentence" else " / ".join(
                    str(v) for v in (item.matrix_name, item.paper_title, item.column_label) if v)
                typer.echo(f"{item.target_id} ({item.target_kind}): {context}: {item.text}")


def _review(ctx: typer.Context, target_id: str, action: Literal["accept", "reject", "edit"],
            value: str | None, reason: str) -> None:
    with _context(ctx) as (_, conn, _):
        kind: Literal["sentence", "cell"] = "sentence"
        if conn.execute("SELECT 1 FROM matrix_cells WHERE id=?", (target_id,)).fetchone():
            kind = "cell"
        parsed: object = value
        if kind == "cell" and value is not None:
            with suppress(json.JSONDecodeError):
                parsed = json.loads(value)
        record_review(conn, kind, target_id, action, new_value=parsed, reason=reason)
        typer.echo(f"{action}: {target_id}")


@review_app.command("accept")
def review_accept(ctx: typer.Context, target_id: str, reason: Reason = "") -> None:
    _review(ctx, target_id, "accept", None, reason)


@review_app.command("reject")
def review_reject(ctx: typer.Context, target_id: str, reason: Reason = "") -> None:
    _review(ctx, target_id, "reject", None, reason)


@review_app.command("edit")
def review_edit(ctx: typer.Context, target_id: str, value: str, reason: Reason = "") -> None:
    _review(ctx, target_id, "edit", value, reason)


def _matrix_id(conn: sqlite3.Connection, name: str, pid: str | None) -> str:
    # Names are scoped to the selected project; IDs allow explicit cross-project access.
    row = conn.execute("SELECT id FROM matrices WHERE id=?", (name,)).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM matrices WHERE name=? AND project_id IS ?", (name, pid)).fetchone()
    if row is None:
        raise ValueError(f"matrix not found: {name}")
    return str(row["id"])


@matrix_app.command("create")
def matrix_create(ctx: typer.Context, name: str, question: Annotated[str, typer.Option("--question")],
                  columns: Annotated[Path, typer.Option("--columns")], project: Project = None) -> None:
    with _context(ctx, project) as (_, conn, pid):
        matrix = create_matrix(conn, pid, name, question, load_columns(columns))
        typer.echo(f"{matrix.name}: {matrix.id}")


@matrix_app.command("add-column")
def matrix_add_column(ctx: typer.Context, name: str, key: str,
                      label: Annotated[str, typer.Option("--label")],
                      instruction: Annotated[str, typer.Option("--instruction")],
                      type: Annotated[str, typer.Option("--type")],
                      unit: Annotated[str | None, typer.Option("--unit")] = None,
                      options: Annotated[str | None, typer.Option("--options")] = None,
                      project: Project = None) -> None:
    with _context(ctx, project) as (_, conn, pid):
        column = MatrixColumn.model_validate(dict(key=key, label=label, instruction=instruction, type=type, unit=unit,
                                                  options=options.split(",") if options else None))
        add_column(conn, _matrix_id(conn, name, pid), column)
        typer.echo(f"added {key} to {name}")


@matrix_app.command("extract")
def matrix_extract(ctx: typer.Context, name: str,
                   paper: Annotated[list[str] | None, typer.Option("--paper")] = None,
                   force: Annotated[bool, typer.Option("--force")] = False, profile: Profile = None,
                   approve: Approve = False, preview: Preview = False, project: Project = None) -> None:
    with _context(ctx, project) as (st, conn, pid):
        matrix_id = _matrix_id(conn, name, pid)
        gateway = ModelGateway(conn, st.config)
        check_plan(gateway, matrix_plan(conn, gateway, matrix_id, paper, profile, force), approve, preview)
        result = extract_matrix(conn, gateway, matrix_id, paper, profile, approve, force)
        typer.echo(f"{len(result.cells)} cells; run {result.run_id}")
        typer.echo(export_matrix(conn, matrix_id, "markdown"))


@matrix_app.command("show")
def matrix_show(ctx: typer.Context, name: str, project: Project = None) -> None:
    with _context(ctx, project) as (_, conn, pid):
        typer.echo(export_matrix(conn, _matrix_id(conn, name, pid), "markdown"))


@matrix_app.command("export")
def matrix_export(ctx: typer.Context, name: str,
                  format: Annotated[str, typer.Option("--format")] = "md", out: Output = None,
                  project: Project = None) -> None:
    with _context(ctx, project) as (_, conn, pid):
        content = export_matrix(conn, _matrix_id(conn, name, pid), format)
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
            typer.echo(str(out))
        else:
            typer.echo(content)


@matrix_app.command("list")
def matrix_list(ctx: typer.Context, project: Project = None) -> None:
    with _context(ctx, project) as (_, conn, pid):
        for matrix in list_matrices(conn, pid):
            typer.echo(f"{matrix.name}: {matrix.id} ({len(matrix.columns)} columns, {len(matrix.cells)} cells)")


def brief(ctx: typer.Context, run_id: str, matrix: Annotated[str | None, typer.Option("--matrix")] = None,
          out: Output = None) -> None:
    """Write a cited research brief with method and evidence appendix."""
    with _context(ctx) as (st, conn, _):
        run = get_run(conn, run_id)
        matrix_id = _matrix_id(conn, matrix, run.project_id) if matrix else None
        destination = out or st.paths.exports_root / f"brief-{run_id}.md"
        typer.echo(str(write_brief(conn, run_id, matrix_id, out=destination)))


def eval_answers(ctx: typer.Context, benchmark: Path, profile: Profile = None, approve: Approve = False,
                 project: Project = None, as_json: Json = False) -> None:
    """Measure citation precision and unsupported-sentence rate."""
    with _context(ctx, project) as (st, conn, pid):
        bench = load_benchmark(benchmark)
        gateway = ModelGateway(conn, st.config)
        plan = [p for q in bench.questions for p in answers.plan_calls(conn, gateway, q.question, pid, profile=profile)]
        check_plan(gateway, plan, approve, False)
        report = evaluate_answers(conn, gateway, bench, profile, pid, approve)
        typer.echo(report.model_dump_json(indent=1) if as_json else
                   f"Citation precision: {report.citation_precision:.3f}; "
                   f"unsupported-sentence rate: {report.unsupported_sentence_rate:.3f}; "
                   f"invalid citations: {report.invalid_citation_count}")


def eval_matrix(ctx: typer.Context, benchmark: Path, matrix: Annotated[str, typer.Option("--matrix")],
                project: Project = None, as_json: Json = False) -> None:
    """Measure matrix status/value accuracy and citation validity."""
    with _context(ctx, project) as (_, conn, pid):
        report = evaluate_matrix(conn, load_benchmark(benchmark), _matrix_id(conn, matrix, pid))
        typer.echo(report.model_dump_json(indent=1) if as_json else
                   f"Status accuracy: {report.status_accuracy:.3f}; value accuracy: {report.value_accuracy:.3f}; "
                   f"citation validity: {report.citation_validity:.3f}")


def register(app: typer.Typer, eval_app: typer.Typer) -> None:
    app.add_typer(matrix_app, name="matrix")
    app.add_typer(review_app, name="review")
    app.command("audit")(audit)
    app.command("brief")(brief)
    eval_app.command("answers")(eval_answers)
    eval_app.command("matrix")(eval_matrix)
