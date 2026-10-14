"""LitForge command-line interface."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel

from litforge.config import Config, Paths, load_config, resolve_paths
from litforge.doctor import run_checks
from litforge.export.markdown import export_markdown
from litforge.indexing.fts import current_runs, index_run, search
from litforge.ingest import ingest_paths
from litforge.observability import EventLog
from litforge.parsing.run import artifacts_for_project, parse_artifact
from litforge.projects import ProjectNotFound, create_project, get_project_id, list_projects
from litforge.storage.db import open_db

app = typer.Typer(help="Local-first scientific literature library.", no_args_is_help=True)
project_app = typer.Typer(help="Manage project workspaces.", no_args_is_help=True)
app.add_typer(project_app, name="project")


@dataclass
class State:
    paths: Paths
    config: Config
    log: EventLog

    def db(self) -> sqlite3.Connection:
        self.paths.ensure()
        return open_db(self.paths.db)


def _state(ctx: typer.Context) -> State:
    return ctx.obj  # type: ignore[no-any-return]


def _project_id(conn: sqlite3.Connection, name: str | None) -> str | None:
    if name is None:
        return None
    try:
        return get_project_id(conn, name)
    except ProjectNotFound as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc


def _emit_json(items: Sequence[BaseModel] | Sequence[dict[str, Any]]) -> None:
    data = [i.model_dump() if isinstance(i, BaseModel) else i for i in items]
    typer.echo(json.dumps(data, indent=1, ensure_ascii=False, default=str))


@app.callback()
def main(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Data root (default $LITFORGE_HOME or ./data)")
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="YAML overriding configs/default.yaml")] = None,
) -> None:
    paths = resolve_paths(data_dir)
    ctx.obj = State(paths=paths, config=load_config(config), log=EventLog(paths.logs))


@app.command()
def doctor(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check Python, parsers, SQLite FTS5, GPU, Ollama, disk and schema."""
    checks = run_checks(_state(ctx).paths)
    if as_json:
        _emit_json(checks)
        return
    colors = {"ok": "green", "warn": "yellow", "error": "red"}
    for c in checks:
        typer.secho(f"[{c.level:>5}] {c.name:<10} {c.detail}", fg=colors[c.level])
    if any(c.level == "error" for c in checks):
        raise typer.Exit(1)


@project_app.command("create")
def project_create(
    ctx: typer.Context,
    name: str,
    topic: Annotated[str | None, typer.Option(help="Short topic description")] = None,
    question: Annotated[str | None, typer.Option(help="Research question")] = None,
) -> None:
    """Create a project workspace."""
    st = _state(ctx)
    conn = st.db()
    try:
        create_project(conn, st.paths, name, topic, question)
    except ValueError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"created project '{name}' at {st.paths.project_dir(name)}")


@project_app.command("list")
def project_list(ctx: typer.Context) -> None:
    """List projects with paper counts."""
    for row in list_projects(_state(ctx).db()):
        typer.echo(f"{row['name']:<24} {row['papers']:>5} papers  {row['topic'] or ''}")


@app.command()
def ingest(
    ctx: typer.Context,
    sources: Annotated[list[Path], typer.Argument(help="PDF files or directories", exists=True)],
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Import PDFs into the library (idempotent: identical files are never duplicated)."""
    st = _state(ctx)
    conn = st.db()
    results = ingest_paths(conn, st.paths, sources, _project_id(conn, project), dry_run, st.log)
    if as_json:
        _emit_json(results)
        return
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in ("quarantined", "error") or r.possible_duplicates:
            typer.secho(f"  {r.status:<12} {Path(r.path).name}: {r.message}", fg="yellow")
    prefix = "[dry-run] " if dry_run else ""
    typer.echo(prefix + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no PDFs found")


@app.command()
def parse(
    ctx: typer.Context,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-parse even when cached")] = False,
    no_docling: Annotated[bool, typer.Option("--no-docling", help="Never run the Docling fallback")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Parse ingested PDFs (cached by PDF hash + parser version + config; resumable)."""
    st = _state(ctx)
    conn = st.db()
    artifacts = artifacts_for_project(conn, _project_id(conn, project))
    if dry_run:
        typer.echo(f"[dry-run] {len(artifacts)} artifact(s) would be parsed or served from cache")
        return
    results = []
    for i, art in enumerate(artifacts, 1):
        res = parse_artifact(conn, st.paths, st.config, art, force, not no_docling, st.log)
        results.append(res)
        if not as_json:
            color = "green" if res.status == "ok" else "yellow" if res.status != "failed" else "red"
            note = f" — {res.message}" if res.message else ""
            reasons = "" if res.status == "ok" else f" ({'; '.join(res.route_reasons)})"
            typer.secho(f"[{i}/{len(artifacts)}] {res.outcome:<7} {res.status:<14} "
                        f"{art['sha256'][:10]}{reasons}{note}", fg=color)
    if as_json:
        _emit_json(results)


@app.command()
def index(
    ctx: typer.Context,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Chunk current parses and build the FTS5 index."""
    st = _state(ctx)
    conn = st.db()
    results = [index_run(conn, st.paths, st.config, run, force)
               for run in current_runs(conn, _project_id(conn, project))]
    if as_json:
        _emit_json(results)
        return
    indexed = sum(r.outcome == "indexed" for r in results)
    total = sum(r.chunks for r in results)
    typer.echo(f"{len(results)} paper(s): {indexed} indexed, {len(results) - indexed} cached; {total} chunks")


@app.command("search")
def search_cmd(
    ctx: typer.Context,
    query: str,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    max_per_paper: Annotated[int, typer.Option(help="Cap hits per paper")] = 3,
    raw: Annotated[bool, typer.Option("--raw", help="Pass query as raw FTS5 syntax")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Lexical (BM25) search; every hit carries paper, section, page and a quoted snippet."""
    conn = _state(ctx).db()
    hits = search(conn, query, _project_id(conn, project), limit, max_per_paper, raw)
    if as_json:
        _emit_json(hits)
        return
    if not hits:
        typer.echo("no results")
    for n, h in enumerate(hits, 1):
        typer.secho(f"{n}. {h.title or h.paper_id}", bold=True)
        typer.echo(f"   p.{h.page} (chunk pp.{h.page_start}-{h.page_end}) · {h.section} · {h.kind}"
                   f" · bm25 {h.score}")
        typer.echo(f"   “{h.snippet}”")


@app.command()
def status(ctx: typer.Context, project: Annotated[str | None, typer.Option("--project", "-p")] = None) -> None:
    """Summarize library or project state."""
    conn = _state(ctx).db()
    pid = _project_id(conn, project)
    scope = "WHERE a.paper_id IN (SELECT paper_id FROM project_papers WHERE project_id=:pid)" if pid else ""
    art = conn.execute(f"SELECT a.status, COUNT(*) n FROM artifacts a {scope} GROUP BY a.status",
                       {"pid": pid}).fetchall()
    runs = conn.execute(
        f"""SELECT r.status, r.parser, COUNT(*) n FROM parse_runs r JOIN artifacts a ON a.id = r.artifact_id
            {scope + ' AND' if scope else 'WHERE'} r.is_current = 1 GROUP BY r.status, r.parser""",
        {"pid": pid},
    ).fetchall()
    chunks = conn.execute(
        f"""SELECT COUNT(*) FROM chunks c JOIN parse_runs r ON r.id = c.parse_run_id AND r.is_current = 1
            JOIN artifacts a ON a.id = r.artifact_id {scope}""", {"pid": pid},
    ).fetchone()[0]
    dupes = conn.execute("SELECT COUNT(*) FROM duplicate_candidates WHERE status='pending'").fetchone()[0]
    typer.echo(f"scope: {project or 'whole library'}")
    typer.echo("artifacts: " + (", ".join(f"{r['status']} {r['n']}" for r in art) or "none"))
    typer.echo("current parses: " + (", ".join(f"{r['status']}/{r['parser']} {r['n']}" for r in runs) or "none"))
    typer.echo(f"indexed chunks: {chunks}")
    typer.echo(f"possible duplicates awaiting review (library-wide): {dupes}")


@app.command()
def export(
    ctx: typer.Context,
    project: Annotated[str, typer.Option("--project", "-p")],
    fmt: Annotated[str, typer.Option("--format", help="Export format (markdown)")] = "markdown",
) -> None:
    """Export a project's papers as Markdown with relative asset links."""
    st = _state(ctx)
    conn = st.db()
    pid = _project_id(conn, project)
    assert pid is not None
    if fmt != "markdown":
        typer.secho(f"format '{fmt}' not supported yet; available: markdown", fg="red", err=True)
        raise typer.Exit(2)
    out = export_markdown(conn, st.paths, project, pid)
    typer.echo(f"exported to {out}")


if __name__ == "__main__":
    app()
