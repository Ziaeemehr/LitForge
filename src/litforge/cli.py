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

from litforge.config import Config, EnvSettings, Paths, load_config, resolve_paths
from litforge.discovery.fetch import fetch_open_access
from litforge.discovery.http import HttpClient
from litforge.discovery.run import DiscoveryReport, Sources, enrich_project, run_citations, run_import, run_search
from litforge.discovery.similar import run_similar
from litforge.discovery.textsim import load_embedder
from litforge.doctor import run_checks
from litforge.export.markdown import export_markdown
from litforge.indexing.fts import current_runs, index_run, search
from litforge.ingest import ingest_paths
from litforge.observability import EventLog
from litforge.parsing.run import artifacts_for_project, parse_artifact
from litforge.pipeline import add_pdfs
from litforge.projects import (
    MARK_SCOPES,
    ProjectNotFound,
    create_project,
    delete_project,
    get_active,
    get_project_id,
    list_projects,
    set_active,
)
from litforge.screening.llm import Llm, LlmError, ollama_from_config
from litforge.screening.run import screen_run
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


ProjectOpt = Annotated[str | None, typer.Option("--project", "-p", help="Project (default: the active project)")]


def _project(
    st: State, conn: sqlite3.Connection, name: str | None, required: bool = False
) -> tuple[str | None, str | None]:
    """Resolve (name, id): explicit --project, else the active project, else None (whole library)."""
    name = name or get_active(st.paths)
    if name is None:
        if required:
            typer.secho("no project selected; create one with `litforge new NAME`", fg="red", err=True)
            raise typer.Exit(2)
        return None, None
    try:
        return name, get_project_id(conn, name)
    except ProjectNotFound as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc


def _create(st: State, name: str, topic: str | None, question: str | None) -> None:
    try:
        create_project(st.db(), st.paths, name, topic, question)
    except ValueError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    set_active(st.paths, name)
    typer.echo(f"created project '{name}' (now active) at {st.paths.project_dir(name)}")


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
    """Create a project and make it active."""
    _create(_state(ctx), name, topic, question)


@app.command()
def new(
    ctx: typer.Context,
    name: str,
    topic: Annotated[str | None, typer.Option(help="Short topic description")] = None,
) -> None:
    """Create a project and make it active (shortcut for `project create`)."""
    _create(_state(ctx), name, topic, None)


@project_app.command("use")
def project_use(ctx: typer.Context, name: str) -> None:
    """Make an existing project active, so --project can be omitted."""
    st = _state(ctx)
    _project(st, st.db(), name, required=True)
    set_active(st.paths, name)
    typer.echo(f"active project: {name}")


@project_app.command("delete")
def project_delete(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation")] = False,
    delete_exports: Annotated[bool, typer.Option("--delete-exports", help="Also delete its exported notes and PDFs")]
    = False,
) -> None:
    """Delete a project (papers and PDFs stay in the library; exports are kept unless --delete-exports)."""
    st = _state(ctx)
    conn = st.db()
    _project(st, conn, name, required=True)
    if not yes:
        typer.confirm(f"Delete project '{name}' with its runs and marks?", abort=True)
    res = delete_project(conn, st.paths, name, delete_exports)
    typer.echo(f"deleted '{name}' ({res['papers']} papers stay in the library)")
    if res["kept_exports"]:
        typer.echo(f"kept your exports in {res['kept_exports']}")


@project_app.command("list")
def project_list(ctx: typer.Context) -> None:
    """List projects with paper counts (* = active)."""
    st = _state(ctx)
    active = get_active(st.paths)
    for row in list_projects(st.db()):
        mark = "*" if row["name"] == active else " "
        typer.echo(f"{mark} {row['name']:<24} {row['papers']:>5} papers  {row['topic'] or ''}")


@app.command()
def ingest(
    ctx: typer.Context,
    sources: Annotated[list[Path], typer.Argument(help="PDF files or directories", exists=True)],
    project: ProjectOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Import PDFs into the library (idempotent: identical files are never duplicated)."""
    st = _state(ctx)
    conn = st.db()
    results = ingest_paths(conn, st.paths, sources, _project(st, conn, project)[1], dry_run, st.log)
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
def add(
    ctx: typer.Context,
    sources: Annotated[list[Path], typer.Argument(help="PDF files or directories", exists=True)],
    project: ProjectOpt = None,
) -> None:
    """Add PDFs to a project: import, parse and index in one step (safe to re-run)."""
    st = _state(ctx)
    conn = st.db()
    if project is None and get_active(st.paths) is None:
        _create(st, "default", None, None)
    name, pid = _project(st, conn, project, required=True)
    assert pid is not None
    label = ", ".join(src.name for src in sources)[:80]
    _, results, parsed = add_pdfs(conn, st.paths, st.config, pid, sources, label, st.log)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in ("quarantined", "error"):
            typer.secho(f"  skipped {Path(r.path).name}: {r.message}", fg="yellow")
    typer.echo(f"imported into '{name}': " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no PDFs"))
    statuses: dict[str, int] = {}
    for res in parsed:
        statuses[res.status or "?"] = statuses.get(res.status or "?", 0) + 1
    typer.echo("parsed: " + (", ".join(f"{k} {v}" for k, v in sorted(statuses.items())) or "nothing"))
    chunks = sum(index_run(conn, st.paths, st.config, run).chunks for run in current_runs(conn, pid))
    typer.echo(f"indexed: {chunks} chunks. Try: litforge search \"your query\"")


@app.command()
def parse(
    ctx: typer.Context,
    project: ProjectOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Re-parse even when cached")] = False,
    no_docling: Annotated[bool, typer.Option("--no-docling", help="Never run the Docling fallback")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Parse ingested PDFs (cached by PDF hash + parser version + config; resumable)."""
    st = _state(ctx)
    conn = st.db()
    artifacts = artifacts_for_project(conn, _project(st, conn, project)[1])
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
    project: ProjectOpt = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Chunk current parses and build the FTS5 index."""
    st = _state(ctx)
    conn = st.db()
    results = [index_run(conn, st.paths, st.config, run, force)
               for run in current_runs(conn, _project(st, conn, project)[1])]
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
    project: ProjectOpt = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    max_per_paper: Annotated[int, typer.Option(help="Cap hits per paper")] = 3,
    raw: Annotated[bool, typer.Option("--raw", help="Pass query as raw FTS5 syntax")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Lexical (BM25) search; every hit carries paper, section, page and a quoted snippet."""
    st = _state(ctx)
    conn = st.db()
    hits = search(conn, query, _project(st, conn, project)[1], limit, max_per_paper, raw)
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
    st = _state(ctx)
    conn = st.db()
    project, pid = _project(st, conn, project)
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
    paper_scope = "WHERE p.id IN (SELECT paper_id FROM project_papers WHERE project_id=:pid)" if pid else ""
    n_papers, n_pdf = conn.execute(
        f"""SELECT COUNT(*), SUM(EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status='ok'))
            FROM papers p {paper_scope}""", {"pid": pid},
    ).fetchone()
    typer.echo(f"scope: {project or 'whole library'}")
    typer.echo(f"papers: {n_papers} ({n_pdf or 0} with PDF)")
    typer.echo("artifacts: " + (", ".join(f"{r['status']} {r['n']}" for r in art) or "none"))
    typer.echo("current parses: " + (", ".join(f"{r['status']}/{r['parser']} {r['n']}" for r in runs) or "none"))
    typer.echo(f"indexed chunks: {chunks}")
    typer.echo(f"possible duplicates awaiting review (library-wide): {dupes}")


@app.command()
def export(
    ctx: typer.Context,
    project: ProjectOpt = None,
    fmt: Annotated[str, typer.Option("--format", help="Export format (markdown)")] = "markdown",
    only: Annotated[
        str, typer.Option("--only", help="Which papers: all, relevant (marked ✓) or not-irrelevant (all but ✗)")
    ] = "all",
    with_pdfs: Annotated[
        bool, typer.Option("--fetch", help="First download open-access PDFs of these papers (full-text notes)")
    ] = False,
) -> None:
    """Export a project's papers as Markdown with relative asset links."""
    st = _state(ctx)
    conn = st.db()
    name, pid = _project(st, conn, project, required=True)
    assert name is not None and pid is not None
    if fmt != "markdown":
        typer.secho(f"format '{fmt}' not supported yet; available: markdown", fg="red", err=True)
        raise typer.Exit(2)
    if only not in MARK_SCOPES:
        typer.secho(f"--only must be one of: {', '.join(MARK_SCOPES)}", fg="red", err=True)
        raise typer.Exit(2)
    if with_pdfs:
        _fetch(st, conn, pid, only)
    out, count = export_markdown(conn, st.paths, name, pid, only=only)
    typer.echo(f"exported {count} papers to {out}")


# ---- discovery ------------------------------------------------------------


def _sources(st: State, refresh: bool) -> Sources:
    env = EnvSettings()
    http = HttpClient(cache_dir=st.paths.http_cache, cache_ttl_s=st.config.discovery.cache_ttl_days * 86400,
                      refresh=refresh)
    return Sources(http, env.openalex_api_key, env.mailto or st.config.discovery.mailto)


def _print_report(report: DiscoveryReport) -> None:
    for name, n in sorted(report.per_source.items()):
        typer.echo(f"  {name:<10} {n} results")
    for err in report.errors:
        typer.secho(f"  error: {err}", fg="yellow")
    typer.echo(f"{report.papers} papers ({report.new_to_project} new to the project, {report.created} new to the "
               f"library); {report.requests} requests, {report.cache_hits} from cache"
               + (f", ${report.cost_usd:.4f} API cost" if report.cost_usd else ""))
    for n, p in enumerate(report.top, 1):
        flags = ("new " if p.new_to_project else "") + ("pdf" if p.has_pdf else "")
        ident = p.doi or (f"arXiv:{p.arxiv_id}" if p.arxiv_id else "")
        typer.echo(f"{n:>3}. [{p.priority:.2f}] {p.title or '(no title)'} ({p.year or '?'})"
                   f"  relevance {p.score:.2f} · importance {p.importance:.2f}")
        typer.echo(f"       {ident}  {'/'.join(p.sources)}  {flags}".rstrip())
        if p.reasons:
            typer.echo(f"       why: {'; '.join(p.reasons)}")
        if p.importance_notes:
            typer.echo(f"       impact: {'; '.join(p.importance_notes)}")


@app.command()
def discover(
    ctx: typer.Context,
    queries: Annotated[list[str] | None, typer.Argument(help="Search queries (default: the project topic)")] = None,
    project: ProjectOpt = None,
    source: Annotated[list[str] | None, typer.Option("--source", "-s", help="openalex, crossref, arxiv")] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Results per source and query")] = None,
    seed: Annotated[list[str] | None, typer.Option(help="DOI, arXiv id or paper id to follow citations from")] = None,
    citations: Annotated[str, typer.Option(help="With --seed: references, cited_by or both")] = "both",
    top: Annotated[int, typer.Option(help="How many ranked results to show")] = 20,
    refresh: Annotated[bool, typer.Option("--refresh", help="Ignore cached API responses")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Find papers by search queries and/or by following citations of seed papers."""
    st = _state(ctx)
    conn = st.db()
    name, pid = _project(st, conn, project, required=True)
    assert pid is not None
    srcs = _sources(st, refresh)
    cfg = st.config.discovery
    topic_row = conn.execute("SELECT topic, research_question FROM projects WHERE id=?", (pid,)).fetchone()
    topic = topic_row["research_question"] or topic_row["topic"]
    reports = []
    if seed:
        if citations not in ("references", "cited_by", "both"):
            typer.secho("--citations must be references, cited_by or both", fg="red", err=True)
            raise typer.Exit(2)
        reports.append(run_citations(conn, srcs, pid, seed, citations,  # type: ignore[arg-type]
                                     limit or cfg.limit, top, topic))
    if queries or not seed:
        terms = queries or ([topic] if topic else [])
        if not terms:
            typer.secho("give a query, or set a topic with `litforge new NAME --topic ...`", fg="red", err=True)
            raise typer.Exit(2)
        reports.append(run_search(conn, srcs, pid, terms, source or cfg.sources, limit or cfg.limit, top))
    if as_json:
        _emit_json(reports)
        return
    for report in reports:
        typer.secho(f"{report.kind} in '{name}' ({report.status})", bold=True)
        _print_report(report)


def _make_llm(config: Config, model: str | None) -> Llm:
    return ollama_from_config(config.screening, model)


@app.command()
def screen(
    ctx: typer.Context,
    run: Annotated[str | None, typer.Option("--run", help="Run id (default: the project's latest run)")] = None,
    top: Annotated[int | None, typer.Option(help="How many top papers of the run to screen")] = None,
    model: Annotated[str | None, typer.Option(help="Ollama model (default: screening.model)")] = None,
    project: ProjectOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Screen a run's top papers against the project's criteria with a local model (set criteria in the web UI)."""
    st = _state(ctx)
    conn = st.db()
    name, pid = _project(st, conn, project, required=True)
    if run is None:
        row = conn.execute("SELECT id FROM discovery_runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                           (pid,)).fetchone()
        if row is None:
            typer.secho(f"no runs in '{name}' yet", fg="red", err=True)
            raise typer.Exit(2)
        run = str(row["id"])
    try:
        report = screen_run(conn, _make_llm(st.config, model), run,
                            st.config.screening.default_top if top is None else top,
                            progress=None if as_json else typer.echo, log=st.log)
    except (ValueError, LookupError, LlmError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        _emit_json([report])
        return
    typer.echo(", ".join(f"{k} {v}" for k, v in sorted(report.counts.items())) or "nothing new to screen")


@app.command()
def similar(
    ctx: typer.Context,
    seeds: Annotated[list[str] | None, typer.Argument(help="DOI, arXiv id, OpenAlex id, paper id or PDF file")] = None,
    title: Annotated[str | None, typer.Option(help="Seed paper title")] = None,
    abstract: Annotated[str | None, typer.Option(help="Seed abstract text")] = None,
    abstract_file: Annotated[Path | None, typer.Option(help="Text file with the seed abstract", exists=True)] = None,
    project: ProjectOpt = None,
    top: Annotated[int, typer.Option(help="How many ranked results to show")] = 30,
    no_embeddings: Annotated[bool, typer.Option("--no-embeddings", help="Rank by TF-IDF only")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Ignore cached API responses")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Find papers similar to seed papers (citations, related works, key phrases, text similarity)."""
    st = _state(ctx)
    conn = st.db()
    if abstract_file is not None:
        abstract = abstract_file.read_text(encoding="utf-8")
    if not seeds and not title and not abstract:
        typer.secho("give a seed (DOI, arXiv id, PDF ...) or --title/--abstract", fg="red", err=True)
        raise typer.Exit(2)
    if project is None and get_active(st.paths) is None:
        _create(st, "default", None, None)
    name, pid = _project(st, conn, project, required=True)
    assert pid is not None
    cfg = st.config.similar
    embedder = None
    if not no_embeddings and cfg.embedding_model:
        embedder = load_embedder(cfg.embedding_model, st.paths.model_cache, cfg.embedding_device)
        if embedder is None:
            typer.secho("embeddings unavailable (install with `uv sync --extra embed`); using TF-IDF", fg="yellow")
    report = run_similar(conn, st.paths, st.config, _sources(st, refresh), pid, seeds, title, abstract,
                         embedder, top, st.log)
    if as_json:
        _emit_json([report])
        return
    typer.secho(f"similar papers in '{name}' ({report.status})", bold=True)
    _print_report(report)


@app.command("import")
def import_refs(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help=".bib, .ris or .csv exported from Zotero, Elicit, etc.", exists=True)],
    project: ProjectOpt = None,
) -> None:
    """Import a reference list into the project (papers without PDFs are kept as records)."""
    st = _state(ctx)
    conn = st.db()
    _, pid = _project(st, conn, project, required=True)
    assert pid is not None
    _print_report(run_import(conn, pid, file, top_n=0))


@app.command()
def enrich(
    ctx: typer.Context,
    project: ProjectOpt = None,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
) -> None:
    """Complete missing DOI, year, venue and abstract from OpenAlex/arXiv; verify DOIs."""
    st = _state(ctx)
    conn = st.db()
    _, pid = _project(st, conn, project, required=True)
    assert pid is not None
    res = enrich_project(conn, _sources(st, refresh), pid)
    filled = ", ".join(f"{k} {v}" for k, v in sorted(res.filled.items())) or "nothing"
    typer.echo(f"checked {res.checked}, enriched {res.enriched}, not found {res.not_found}; filled: {filled}"
               + (f"; ${res.cost_usd:.4f} API cost" if res.cost_usd else ""))
    for err in res.errors:
        typer.secho(f"  error: {err}", fg="yellow")


@app.command()
def fetch(
    ctx: typer.Context,
    project: ProjectOpt = None,
    only: Annotated[
        str, typer.Option("--only", help="Which papers: relevant (marked ✓), not-irrelevant (all but ✗) or all")
    ] = "relevant",
) -> None:
    """Download open-access PDFs (arXiv, publisher OA copies), then parse and index them."""
    st = _state(ctx)
    conn = st.db()
    _, pid = _project(st, conn, project, required=True)
    assert pid is not None
    if only not in MARK_SCOPES:
        typer.secho(f"--only must be one of: {', '.join(MARK_SCOPES)}", fg="red", err=True)
        raise typer.Exit(2)
    _fetch(st, conn, pid, only)


def _fetch(st: State, conn: sqlite3.Connection, pid: str, only: str) -> None:
    report = fetch_open_access(conn, st.paths, st.config, HttpClient(), pid, only, log=st.log,
                               progress=typer.echo)
    typer.echo(f"downloaded {report.downloaded} of {report.candidates} papers without a PDF, "
               f"{report.parsed} with full text; {report.no_source} have no open-access copy")
    for err in report.failed:
        typer.secho(f"  failed: {err}", fg="yellow")


@app.command()
def papers(
    ctx: typer.Context,
    project: ProjectOpt = None,
    no_pdf: Annotated[bool, typer.Option("--no-pdf", help="Only papers without a PDF")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the project's papers, best discovery score first."""
    st = _state(ctx)
    conn = st.db()
    _, pid = _project(st, conn, project, required=True)
    rows = conn.execute(
        f"""SELECT p.id, p.title, p.year, p.doi, p.arxiv_id, p.oa_url, pp.source,
                   EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status='ok') AS has_pdf,
                   (SELECT MAX(score) FROM discovery_results dr JOIN discovery_runs r ON r.id = dr.run_id
                    WHERE dr.paper_id = p.id AND r.project_id = pp.project_id) AS score
            FROM project_papers pp JOIN papers p ON p.id = pp.paper_id
            WHERE pp.project_id = ? {"AND has_pdf = 0" if no_pdf else ""}
            ORDER BY score IS NULL, score DESC, p.year DESC LIMIT ?""",
        (pid, limit),
    ).fetchall()
    if as_json:
        _emit_json([dict(r) for r in rows])
        return
    for r in rows:
        pdf = "pdf" if r["has_pdf"] else ("oa " if r["oa_url"] else "   ")
        score = f"{r['score']:.2f}" if r["score"] is not None else "  - "
        typer.echo(f"{score} {pdf} {r['year'] or '????'}  {(r['title'] or '(no title)')[:90]}")


@app.command()
def ui(
    ctx: typer.Context,
    port: Annotated[int, typer.Option(help="Port (0 = pick a free one)")] = 8765,
    host: Annotated[str, typer.Option(help="Interface to bind (keep 127.0.0.1)")] = "127.0.0.1",
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Don't open a browser window")] = False,
) -> None:
    """Open the web interface (runs locally; works the same on macOS and Linux)."""
    from litforge.ui.server import serve

    st = _state(ctx)
    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(f"warning: serving on {host}; anyone who can reach this address can use the UI", fg="yellow")
    serve(st.paths, st.config, host, port, not no_browser)


if __name__ == "__main__":
    app()
