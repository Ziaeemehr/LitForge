"""LitForge command-line interface."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import BaseModel

from litforge.analysis.commands import register, run_ask
from litforge.analysis.evaluation import evaluate_retrieval, load_benchmark
from litforge.analysis.gateway import GatewayError, ModelGateway
from litforge.analysis.ledger import record_spans
from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.runs import finish_run, link_evidence, start_run
from litforge.config import Config, EnvSettings, Paths, contact_email, load_config, resolve_paths
from litforge.console.commands import register as register_console
from litforge.discovery.fetch import fetch_open_access
from litforge.discovery.http import HttpClient
from litforge.discovery.run import DiscoveryReport, Sources, enrich_project, run_citations, run_import, run_search
from litforge.discovery.similar import run_similar
from litforge.discovery.textsim import load_embedder
from litforge.discovery.versions import (
    backfill_unknown_versions,
    confirm_version_link,
    link_project_versions,
    pdf_warning,
    primary_paper_ids,
    reject_version_link,
    set_artifact_version,
    set_primary,
    unlink_version,
)
from litforge.doctor import run_checks
from litforge.export.markdown import export_markdown
from litforge.indexing.fts import current_runs, index_run, search
from litforge.ingest import ingest_paths
from litforge.library_cleanup import RemovalPreview, plan_removal, remove_papers
from litforge.llm.budget import BudgetExceeded
from litforge.llm.commands import register as register_models
from litforge.llm.router import RoutingError
from litforge.observability import EventLog
from litforge.parsing.display import parse_status_label, parser_install_hint
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
from litforge.screening.run import preview_run, screen_run
from litforge.storage.db import open_db
from litforge.storage.recovery import repair_abandoned_runs
from litforge.tools import install_pix2text, install_timeout_s, pix2text_install_status
from litforge.updates import check_for_update

app = typer.Typer(help="Local-first scientific literature library.", no_args_is_help=True)
project_app = typer.Typer(help="Manage project workspaces.", no_args_is_help=True)
app.add_typer(project_app, name="project")
library_app = typer.Typer(help="Manage papers in the shared library.", no_args_is_help=True)
app.add_typer(library_app, name="library")
eval_app = typer.Typer(help="Evaluate retrieval quality.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
versions_app = typer.Typer(help="Inspect and manage preprint, accepted and published versions.")
app.add_typer(versions_app, name="versions")
register_models(app)


@dataclass
class State:
    paths: Paths
    config: Config
    log: EventLog

    def db(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(f"{self.paths.db.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
            conn.row_factory = sqlite3.Row
            return conn
        self.paths.ensure()
        conn = open_db(self.paths.db)
        repair_abandoned_runs(conn)
        return conn


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
    ctx.obj = State(paths=paths, config=load_config(config, data_root=paths.root), log=EventLog(paths.logs))


@app.command()
def update(ctx: typer.Context, check: Annotated[bool, typer.Option('--check')] = False) -> None:
    """Check for updates and print the terminal command; never install or pull."""
    result = check_for_update(_state(ctx).paths, force=True)
    typer.echo(f"[{result['status']}] {result['message']}")
    typer.echo('Close LitForge first, then run this in a terminal:')
    typer.echo(result['command'])
    if check:
        raise typer.Exit({'up_to_date': 0, 'available': 10}.get(result['status'], 1))
    if result['status'] == 'error':
        raise typer.Exit(1)


@app.command()
def doctor(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check Python, parsers, SQLite FTS5, GPU, Ollama, disk and schema."""
    checks = run_checks(_state(ctx).paths, _state(ctx).config)
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


def _print_removal(preview: RemovalPreview) -> None:
    typer.echo(f"{preview.paper_count} papers · {preview.pdf_count} PDFs · "
               f"{preview.parsed_folder_count} parsed folders · {preview.bytes_freed / 1_000_000:.2f} MB")
    for paper in preview.papers:
        typer.echo(f"  {paper.id}  {paper.title or '(no title)'}")
    for refusal in preview.refused:
        typer.echo(f"Refused {refusal.id}: {refusal.reason}")
    for error in preview.file_errors:
        typer.echo(f"Files kept {error.path}: {error.reason}")


@library_app.command("remove-unused")
def library_remove_unused(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option('--yes', '-y', help="Don't ask for confirmation")] = False,
    paper: Annotated[list[str] | None, typer.Option('--paper', help='Paper ID (repeatable; default: all unused)')]
    = None,
) -> None:
    """Permanently remove papers that belong to no project and have no cited evidence."""
    st = _state(ctx)
    conn = st.db()
    try:
        preview = plan_removal(conn, st.paths, paper)
        _print_removal(preview)
        if not preview.paper_count:
            return
        if not yes:
            typer.confirm('Remove these papers and their PDFs and parsed text? This is irreversible.', abort=True)
        report = remove_papers(conn, st.paths, [p.id for p in preview.papers])
        typer.echo(f'Removed {len(report.removed)} papers; freed {report.bytes_freed / 1_000_000:.2f} MB')
        for refusal in report.refused:
            typer.echo(f'Refused {refusal.id}: {refusal.reason}')
        for error in report.file_errors:
            typer.echo(f'Files kept {error.path}: {error.reason}')
    finally:
        conn.close()


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
    hint = parser_install_hint(statuses, st.paths, st.config)
    typer.echo("parsed: " + (", ".join(f"{parse_status_label(k)} {v}" for k, v in sorted(statuses.items()))
                            or "nothing") + (f"; {hint}" if hint else ""))
    chunks = sum(index_run(conn, st.paths, st.config, run).chunks for run in current_runs(conn, pid))
    typer.echo(f"indexed: {chunks} chunks. Try: litforge search \"your query\"")


class ParserChoice(StrEnum):
    auto = "auto"
    pymupdf = "pymupdf"
    nemotron = "nemotron"
    mineru = "mineru"
    docling = "docling"


parse_app = typer.Typer(help="Parse PDFs or install the accurate parser.", invoke_without_command=True)
app.add_typer(parse_app, name="parse")


@parse_app.command("install-mineru")
def install_mineru_command(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    _install_mineru(_state(ctx), yes)


def _install_mineru(st: State, yes: bool) -> None:
    from litforge.parsing.mineru_parser import INSTALL_NOTICE, install_mineru

    typer.echo(INSTALL_NOTICE)
    typer.echo(f"Tool: {st.paths.root / 'tools' / 'mineru'}; models: {st.paths.root / 'models' / 'mineru'}")
    if not yes and not typer.confirm("Download and install MinerU?"):
        return
    install_mineru(st.paths, typer.echo, timeout_s=st.config.parsing.mineru.install_timeout_s)


tools_app = typer.Typer(help="Check and install optional LitForge tools.", invoke_without_command=True)
app.add_typer(tools_app, name="tools")
tools_install_app = typer.Typer(help="Install optional tools.", no_args_is_help=True)
tools_app.add_typer(tools_install_app, name="install")


@tools_app.callback()
def tools_status_command(ctx: typer.Context) -> None:
    """Show optional parser, math and vision-model status."""
    if ctx.invoked_subcommand:
        return
    from litforge.ui.server import App

    st_config = _state(ctx).config
    status = App(_state(ctx).paths, st_config).optional_tools_status()
    mineru = status["mineru"]
    pix2text = status["pix2text"]
    vision = status["vision"]
    tectonic = status.get("tectonic", {}).get("tectonic", {})
    vision_name = vision.get("model") or vision.get("message") or "Not configured"
    vision_state = f"{'Ready' if vision.get('ready') else 'Not ready'} · {vision_name}"
    nemo = status.get("nemotron") or {"installed": False, "offerable": False, "reason": "unknown", "size": ""}
    engines = status.get("engines") or {"parse": st_config.parsing.complex_fallback,
                                        "page": st_config.vision.engine}
    rows = (
        ("MinerU", f"Installed · {mineru['version']}" if mineru["installed"] else "Not installed", "~4 GB"),
        ("Nemotron", "Set up" if nemo["installed"] else "Not set up" if nemo["offerable"] else
         f"Not available: {nemo['reason']}", nemo["size"]),
        ("Pix2Text", f"Installed · {pix2text['version']}" if pix2text["installed"] else
         "Not installed" if pix2text["available"] else "Not available on this install", pix2text["size"]),
        ("Image model", str(vision_state),
         str(vision.get("notice") or "model-dependent")),
        ("Tectonic", f"Installed · {tectonic.get('version')}" if tectonic.get("installed") else "Not installed",
         str(tectonic.get("size") or "not available for this platform")),
    )
    typer.echo(f"{'Tool':<14} {'Status':<42} Size / detail")
    for name, state, size in rows:
        typer.echo(f"{name:<14} {state:<42} {size}")
    if not pix2text["installed"] and not pix2text["offerable"]:
        typer.echo(f"Pix2Text command: {pix2text['command']}")
    typer.echo(f"Engines: broken PDFs {engines['parse']}, Math: high pages {engines['page']} "
               "(parsing.complex_fallback and vision.engine in config.yaml)")


@tools_install_app.command("pix2text")
def install_pix2text_command(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation")] = False,
) -> None:
    """Install Pix2Text for Math: medium (~2 GB)."""
    status = pix2text_install_status()
    if not status["offerable"]:
        typer.secho(f"Cannot install Pix2Text here. Run `{status['command']}` in the LitForge source root.",
                    fg="yellow", err=True)
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Install Pix2Text for Math: medium ({status['size']})?"):
        return
    try:
        result = install_pix2text(typer.echo, timeout_s=install_timeout_s())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        typer.secho(f"Pix2Text installation failed: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc
    if result["installed"]:
        typer.echo(f"Pix2Text {result['version']} installed.")
    else:
        typer.echo("Installation finished; restart LitForge to use Pix2Text.")


@tools_install_app.command("tectonic")
def install_tectonic_command(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation")] = False,
) -> None:
    """Install Tectonic, the LaTeX engine of the Writing Studio (~22 MB, packages download on first use)."""
    from litforge.writing.compile import CompileError, engine_status, install_tectonic

    paths = _state(ctx).paths
    status = engine_status(paths)["tectonic"]
    if status["installed"]:
        typer.echo(f"Tectonic {status['version']} is already installed at {status['path']}.")
        return
    if not status["offerable"]:
        typer.secho("No Tectonic build for this platform; install TeX Live or MacTeX instead.", fg="yellow", err=True)
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Download Tectonic {status['release']} ({status['size']})?"):
        return
    try:
        install_tectonic(paths, typer.echo)
    except (OSError, CompileError) as exc:
        typer.secho(f"Tectonic installation failed: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc


write_app = typer.Typer(help="Writing Studio: LaTeX documents in a project.", no_args_is_help=True)
app.add_typer(write_app, name="write")


@write_app.command("list")
def write_list(ctx: typer.Context, project: ProjectOpt = None) -> None:
    """List the project's documents."""
    from litforge.writing.documents import list_documents

    st = _state(ctx)
    conn = st.db()
    name, _ = _project(st, conn, project, required=True)
    assert name is not None
    for doc in list_documents(conn, st.paths, name):
        typer.echo(f"{doc.id}  {doc.name}  ({doc.template}, {doc.engine})  {doc.folder}")


@write_app.command("new")
def write_new(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Document name")],
    project: ProjectOpt = None,
    template: Annotated[str, typer.Option("--template", help="article or persian")] = "article",
) -> None:
    """Create a document from a template."""
    from litforge.writing.documents import DocumentError, create_document

    st = _state(ctx)
    conn = st.db()
    name, _ = _project(st, conn, project, required=True)
    assert name is not None
    try:
        doc = create_document(conn, st.paths, name, title, template)
    except DocumentError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"created {doc.id} in {doc.folder}")


@write_app.command("compile")
def write_compile(
    ctx: typer.Context,
    document: Annotated[str, typer.Argument(help="Document id (see `litforge write list`)")],
    engine: Annotated[str | None, typer.Option("--engine", help="tectonic or xelatex")] = None,
) -> None:
    """Compile a document to PDF."""
    from litforge.writing.compile import CompileError, compile_document
    from litforge.writing.documents import get_document

    st = _state(ctx)
    conn = st.db()
    try:
        doc = get_document(conn, st.paths, document)
        result = compile_document(doc, st.paths, engine)
    except (LookupError, CompileError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    for item in result.errors:
        typer.secho(f"{item.file or ''}:{item.line or ''}: {item.message}", fg="red", err=True)
    for item in result.warnings:
        typer.echo(f"warning: {item.message}", err=True)
    if not result.ok:
        raise typer.Exit(1)
    typer.echo(f"{doc.folder / str(result.pdf)} ({result.elapsed_ms / 1000:.1f} s, {result.engine}, "
               f"sandbox: {result.sandbox})")


@tools_install_app.command("nemotron")
def install_nemotron_command(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation")] = False,
) -> None:
    """Set up Nemotron Parse on an NVIDIA GPU: two small packages and the model (~2.5 GB)."""
    from litforge.tools import NEMOTRON_SIZE, install_nemotron, nemotron_install_status

    st = _state(ctx)
    status = nemotron_install_status(st.paths)
    if status["installed"]:
        typer.echo("Nemotron Parse is already set up")
        return
    if not status["offerable"]:
        typer.secho(f"Nemotron Parse cannot be set up here: {status['reason']}", fg="red", err=True)
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Download Nemotron Parse ({NEMOTRON_SIZE})?"):
        raise typer.Exit(1)
    try:
        install_nemotron(st.paths, typer.echo)
    except (RuntimeError, ValueError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    typer.echo("done: Nemotron Parse is ready for Math: high and broken PDFs")


@tools_install_app.command("mineru")
def install_mineru_alias(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation")] = False,
) -> None:
    """Install MinerU for accurate PDF parsing (~4 GB)."""
    _install_mineru(_state(ctx), yes)


@parse_app.callback()
def parse(
    ctx: typer.Context,
    project: ProjectOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Re-parse even when cached")] = False,
    no_docling: Annotated[bool, typer.Option("--no-docling", help="Disable structural fallback")] = False,
    parser: Annotated[ParserChoice, typer.Option("--parser")] = ParserChoice.auto,
    paper: Annotated[list[str] | None, typer.Option("--paper", help="Paper ID (repeatable)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Parse ingested PDFs (cached by PDF hash + parser version + config; resumable)."""
    if ctx.invoked_subcommand:
        return
    st = _state(ctx)
    conn = st.db()
    scope = _project(st, conn, project)[1] if project or not paper else None
    artifacts = artifacts_for_project(conn, scope)
    if paper:
        artifacts = [art for art in artifacts if art["paper_id"] in paper]
        missing = set(paper) - {art["paper_id"] for art in artifacts}
        if missing:
            where = "in this project" if scope else "with a PDF in the library"
            typer.secho(f"skipped {len(missing)} paper(s) not {where}: {', '.join(sorted(missing))}",
                        fg="yellow", err=True)
    if dry_run:
        typer.echo(f"[dry-run] {len(artifacts)} artifact(s) would be parsed or served from cache")
        return
    results = []
    for i, art in enumerate(artifacts, 1):
        res = parse_artifact(conn, st.paths, st.config, art, force, not no_docling, st.log, parser=parser.value)
        results.append(res)
        if not as_json:
            color = "green" if res.status == "ok" else "yellow" if res.status != "failed" else "red"
            note = f" — {res.message}" if res.message else ""
            hint = parser_install_hint([res.status], st.paths, st.config)
            if hint and hint not in note:
                note += f"; {hint}"
            reasons = "" if res.status == "ok" else f" ({'; '.join(res.route_reasons)})"
            typer.secho(f"[{i}/{len(artifacts)}] {res.outcome:<7} {parse_status_label(res.status):<14} "
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
def ask(
    ctx: typer.Context,
    question: str,
    evidence_only: Annotated[bool, typer.Option("--evidence-only")] = False,
    project: ProjectOpt = None,
    k: Annotated[int, typer.Option("-k", min=1)] = 8,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    approve: Annotated[bool, typer.Option("--approve")] = False,
    preview: Annotated[bool, typer.Option("--preview")] = False,
    audit: Annotated[bool, typer.Option("--audit/--no-audit")] = True,
    audit_profile: Annotated[str | None, typer.Option("--audit-profile")] = None,
    sensitive: Annotated[bool, typer.Option("--sensitive")] = False,
    all_versions: Annotated[bool, typer.Option("--all-versions", help="Use non-primary versions too")] = False,
) -> None:
    """Answer a research question with evidence citations and an optional audit."""
    if not evidence_only:
        run_ask(ctx, question, project, k, profile, approve, preview, audit, audit_profile, sensitive, as_json,
                all_versions)
        return
    st = _state(ctx)
    with st.db() as conn:
        _, pid = _project(st, conn, project)
        run_id = start_run(conn, "ask_evidence", pid, {"question": question},
                           {**st.config.model_dump(), "k": k}, "ask_evidence_v1")
        try:
            hits = retrieve_for_question(conn, question, pid, k, all_versions=all_versions)
            spans = record_spans(conn, hits)
            link_evidence(conn, run_id, spans, [h.score for h in hits], pages=[h.page for h in hits])
            result = {"run_id": run_id, "spans": [{**s.model_dump(), "page": h.page}
                                                  for s, h in zip(spans, hits, strict=True)]}
            finish_run(conn, run_id, "ok", result)
        except (RoutingError, BudgetExceeded, GatewayError, LlmError) as exc:
            finish_run(conn, run_id, "error", {}, str(exc))
            typer.echo(" ".join(str(exc).splitlines()), err=True)
            raise typer.Exit(1) from exc
        except Exception:
            finish_run(conn, run_id, "error", {}, "evidence retrieval failed")
            raise
    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=1))
        return
    if not spans:
        typer.echo("no results")
    for rank, (span, hit) in enumerate(zip(spans, hits, strict=True), 1):
        typer.echo(f"{rank}. [{span.id}] {span.title or span.paper_id} — {span.section or span.kind}, p.{hit.page}")
        typer.echo(f"   “{hit.snippet}”")


@eval_app.command("retrieval")
def eval_retrieval(
    ctx: typer.Context,
    benchmark: Path,
    project: ProjectOpt = None,
    k: Annotated[int, typer.Option("-k", min=1)] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Measure Recall@k, hit-rate@k and MRR against labelled evidence."""
    st = _state(ctx)
    with st.db() as conn:
        _, pid = _project(st, conn, project)
        try:
            report = evaluate_retrieval(conn, load_benchmark(benchmark), k, pid)
        except (OSError, ValueError, yaml.YAMLError, RoutingError, BudgetExceeded, GatewayError, LlmError) as exc:
            typer.echo(" ".join(str(exc).splitlines()), err=True)
            raise typer.Exit(1) from exc
    if as_json:
        typer.echo(report.model_dump_json(indent=1))
        return
    typer.echo(f"Recall@{k}: {report.recall_at_k:.3f} · hit-rate@{k}: {report.hit_rate_at_k:.3f} · "
               f"MRR: {report.mrr:.3f}")
    for question in report.questions:
        for target in question.missed:
            page = f", p.{target.page}" if target.page is not None else ""
            contains = f", contains: {target.contains}" if target.contains is not None else ""
            typer.echo(f"{question.id}: missed {target.paper}{page}{contains}")


@app.command()
def calls(
    ctx: typer.Context,
    run: Annotated[str | None, typer.Option("--run")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show recent model calls, usage, costs and status."""
    sql = "SELECT * FROM model_calls"
    params: list[object] = []
    if run is not None:
        sql += " WHERE run_id=?"
        params.append(run)
    params.append(limit)
    with _state(ctx).db() as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", params).fetchall()
    if as_json:
        _emit_json([dict(r) for r in rows])
        return
    for row in rows:
        typer.echo(f"{row['provider']}/{row['model']} · {row['task']} · "
                   f"{row['input_tokens']} in/{row['output_tokens']} out · "
                   f"${row['cost_usd']:.6f} · {row['latency_ms']} ms · {row['status']}")


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
    hint = parser_install_hint((r["status"] for r in runs), st.paths, st.config)
    typer.echo("current parses: " + (", ".join(
        f"{parse_status_label(r['status'])}/{r['parser']} {r['n']}" for r in runs) or "none")
        + (f"; {hint}" if hint else ""))
    typer.echo(f"indexed chunks: {chunks}")
    typer.echo(f"possible duplicates awaiting review (library-wide): {dupes}")


@app.command()
def export(
    ctx: typer.Context,
    project: ProjectOpt = None,
    fmt: Annotated[str, typer.Option("--format", help="Export format (markdown or bibtex)")] = "markdown",
    only: Annotated[
        str, typer.Option("--only", help="Which papers: all, relevant (marked ✓) or not-irrelevant (all but ✗)")
    ] = "all",
    with_pdfs: Annotated[
        bool, typer.Option("--fetch", help="First download open-access PDFs of these papers (full-text notes)")
    ] = False,
    all_versions: Annotated[bool, typer.Option("--all-versions", help="Export non-primary versions too")] = False,
) -> None:
    """Export a project's papers as Markdown with relative asset links."""
    st = _state(ctx)
    conn = st.db()
    name, pid = _project(st, conn, project, required=True)
    assert name is not None and pid is not None
    if fmt not in {"markdown", "bibtex"}:
        typer.secho(f"format '{fmt}' not supported yet; available: markdown, bibtex", fg="red", err=True)
        raise typer.Exit(2)
    if only not in MARK_SCOPES:
        typer.secho(f"--only must be one of: {', '.join(MARK_SCOPES)}", fg="red", err=True)
        raise typer.Exit(2)
    if fmt == "bibtex":
        from litforge.writing.bib import export_bibtex

        ids = [str(r[0]) for r in conn.execute(
            f"""SELECT p.id FROM project_papers pp JOIN papers p ON p.id=pp.paper_id
                LEFT JOIN paper_marks m ON m.paper_id=p.id AND m.project_id=pp.project_id
                WHERE pp.project_id=?{MARK_SCOPES[only]} ORDER BY p.year, p.title""", (pid,))]
        typer.echo(export_bibtex(conn, ids), nl=False)
        return
    if with_pdfs:
        _fetch(st, conn, pid, only)
    out, count = export_markdown(conn, st.paths, name, pid, only=only, config=st.config,
                                 all_versions=all_versions)
    typer.echo(f"exported {count} papers to {out}")


# ---- discovery ------------------------------------------------------------


def _sources(st: State, refresh: bool) -> Sources:
    env = EnvSettings()
    http = HttpClient(cache_dir=st.paths.http_cache, cache_ttl_s=st.config.discovery.cache_ttl_days * 86400,
                      refresh=refresh)
    return Sources(http, env.openalex_api_key, contact_email(st.paths, st.config))


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
    fulltext: Annotated[bool, typer.Option("--fulltext")] = False,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    approve: Annotated[bool, typer.Option("--approve")] = False,
    preview: Annotated[bool, typer.Option("--preview")] = False,
    project: ProjectOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Screen a run's top papers against the project's criteria with a local model (set criteria in the web UI)."""
    if fulltext:
        from litforge.analysis.claim_commands import fulltext as run_fulltext
        run_fulltext(ctx, project, top, profile, approve, preview, as_json)
        return
    st = _state(ctx)
    conn = st.db(read_only=preview)
    name, pid = _project(st, conn, project, required=True)
    if run is None:
        row = conn.execute("SELECT id FROM discovery_runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                           (pid,)).fetchone()
        if row is None:
            typer.secho(f"no runs in '{name}' yet", fg="red", err=True)
            raise typer.Exit(2)
        run = str(row["id"])
    gateway = (ModelGateway(conn, st.config) if model is None and
               (profile is not None or "screen" in st.config.models.tasks) else None)
    try:
        if preview:
            if gateway is None:
                config = st.config.model_copy(deep=True)
                config.models.local.provider = "ollama"
                config.models.local.model = model or st.config.screening.model
                config.models.local.remote = False
                config.models.tasks.pop("screen", None)
                gateway = ModelGateway(conn, config)
            gateway.pricing.read_only = True
            plan = preview_run(conn, gateway, run, st.config.screening.default_top if top is None else top,
                               profile if model is None else None)
            typer.echo(json.dumps(plan, ensure_ascii=False, indent=1))
            return
        report = screen_run(conn, _make_llm(st.config, model) if gateway is None else None, run,
                            st.config.screening.default_top if top is None else top,
                            progress=None if as_json else typer.echo, log=st.log,
                            gateway=gateway, profile=profile if gateway else None, approved=approve)
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
    http = HttpClient(cache_dir=st.paths.http_cache,
                      cache_ttl_s=st.config.discovery.cache_ttl_days * 86400)
    report = fetch_open_access(conn, st.paths, st.config, http, pid, only, log=st.log,
                               progress=typer.echo, mailto=contact_email(st.paths, st.config))
    typer.echo(f"downloaded {report.downloaded} of {report.candidates} papers without a PDF, "
               f"{report.parsed} with full text; {report.no_source} have no open-access copy")
    for err in report.failed:
        typer.secho(f"  failed: {err}", fg="yellow")


def _show_versions(conn: sqlite3.Connection, project_id: str | None) -> None:
    selected = primary_paper_ids(conn, project_id)
    sql = """SELECT p.id,p.title,p.doi FROM papers p"""
    params: tuple[object, ...] = ()
    if project_id:
        sql += " JOIN project_papers pp ON pp.paper_id=p.id WHERE pp.project_id=?"
        params = (project_id,)
    sql += " ORDER BY p.title"
    for paper in conn.execute(sql, params):
        versions = conn.execute(
            """SELECT pv.group_id,pv.role,pv.confirmed,pv.rejected,pv.is_primary,
                      p.id,p.title,p.doi,a.version,a.version_source
               FROM paper_versions pv JOIN papers p ON p.id=pv.paper_id
               LEFT JOIN artifacts a ON a.id=(SELECT id FROM artifacts WHERE paper_id=p.id AND status='ok'
                                              ORDER BY created_at DESC LIMIT 1)
               WHERE pv.paper_id=? ORDER BY pv.confirmed DESC,pv.role""", (paper["id"],),
        ).fetchall()
        role = versions[0]["role"] if versions else "unknown"
        pdf = conn.execute("SELECT version,version_source FROM artifacts WHERE paper_id=? AND status='ok' "
                           "ORDER BY created_at DESC LIMIT 1", (paper["id"],)).fetchone()
        primary = "primary" if paper["id"] in selected else "non-primary"
        pdf_label = f"{pdf['version']} ({pdf['version_source']})" if pdf else "no PDF"
        typer.echo(f"{paper['id']}  [{role}; {primary}; PDF: {pdf_label}]  {paper['title'] or '(untitled)'}"
                   + (f"  DOI {paper['doi']}" if paper["doi"] else ""))
        if warning := pdf_warning(conn, paper["id"]):
            typer.secho(f"    WARNING: {warning}", fg="yellow")
        for member in versions:
            if member["id"] != paper["id"]:
                typer.echo(f"  -> {member['id']} [{member['role']}; "
                           f"{'confirmed' if member['confirmed'] else 'suggested'}] "
                           f"{member['title'] or '(untitled)'}")


@versions_app.callback(invoke_without_command=True)
def versions_list(ctx: typer.Context, project: ProjectOpt = None) -> None:
    """List version groups and PDF version labels."""
    if ctx.invoked_subcommand is not None:
        return
    st = _state(ctx)
    conn = st.db(read_only=True)
    try:
        _, pid = _project(st, conn, project)
        _show_versions(conn, pid)
    finally:
        conn.close()


@versions_app.command("link")
def versions_link(ctx: typer.Context, project: ProjectOpt = None) -> None:
    """Link explicit registry versions and record title-based suggestions."""
    st = _state(ctx)
    conn = st.db()
    try:
        # The whole library unless --project names one (the active project does not narrow it).
        pid = _project(st, conn, project)[1] if project else None
        http = HttpClient(cache_dir=st.paths.http_cache,
                          cache_ttl_s=st.config.discovery.cache_ttl_days * 86400)
        linked, suggested = link_project_versions(conn, http, contact_email(st.paths, st.config), pid)
        # PDFs attached before versions were tracked: read their first page (offline, never moves a PDF).
        checked, skipped = backfill_unknown_versions(conn, st.paths)
        typer.echo(f"linked {linked} explicit version relationships; added {suggested} suggestions; "
                   f"labelled {checked} earlier PDFs from their first page"
                   + (f"; skipped {skipped} missing or unreadable PDFs" if skipped else ""))
    finally:
        conn.close()


@versions_app.command("unlink")
def versions_unlink(ctx: typer.Context, paper: str) -> None:
    """Remove a paper from its version group and remember that it is a different work."""
    conn = _state(ctx).db()
    try:
        if not unlink_version(conn, paper):
            raise typer.BadParameter("this paper is not in a confirmed version group")
        typer.echo(f"unlinked versions: {paper}")
    finally:
        conn.close()


@versions_app.command("set-primary")
def versions_set_primary(ctx: typer.Context, paper: str) -> None:
    """Choose a paper as the primary version used by project analysis."""
    conn = _state(ctx).db()
    try:
        set_primary(conn, paper)
        typer.echo(f"primary version: {paper}")
    finally:
        conn.close()


@versions_app.command("confirm")
def versions_confirm(ctx: typer.Context, a: str, b: str) -> None:
    """Confirm that two paper records are versions of one work."""
    conn = _state(ctx).db()
    try:
        if not conn.execute("SELECT 1 FROM papers WHERE id=?", (a,)).fetchone() or not conn.execute(
            "SELECT 1 FROM papers WHERE id=?", (b,)
        ).fetchone():
            raise typer.BadParameter("both paper IDs must exist")
        confirm_version_link(conn, a, b)
        typer.echo(f"linked versions: {a} {b}")
    finally:
        conn.close()


@versions_app.command("reject")
def versions_reject(ctx: typer.Context, a: str, b: str) -> None:
    """Reject and remember a suggested version link."""
    conn = _state(ctx).db()
    try:
        if not reject_version_link(conn, a, b):
            raise typer.BadParameter("no pending suggestion links these papers")
        typer.echo(f"rejected version suggestion: {a} {b}")
    finally:
        conn.close()


@versions_app.command("set")
def versions_set(ctx: typer.Context, paper: str,
                 version: Annotated[str, typer.Option("--version")]) -> None:
    """Override the version label on a paper's current PDF."""
    conn = _state(ctx).db()
    try:
        set_artifact_version(conn, paper, version)
        typer.echo(f"{paper}: PDF version set to {version}")
    finally:
        conn.close()


@app.command()
def enhance(
    ctx: typer.Context,
    project: ProjectOpt = None,
    only: Annotated[
        str, typer.Option("--only", help="Which papers: relevant (marked ✓), not-irrelevant (all but ✗) or all")
    ] = "relevant",
    level: Annotated[str, typer.Option(
        "--level", help="medium: Pix2Text reads the formulas, text from the PDF (fast; GPU if installed). "
                        "high: the local vision model rewrites whole pages (best; also fixes scanned text)")] = "high",
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    approve: Annotated[bool, typer.Option("--approve")] = False,
    preview: Annotated[bool, typer.Option("--preview")] = False,
) -> None:
    """Better math: rewrite pages with equations, tables or broken text (the next export uses them)."""
    from litforge.parsing.formulas import FormulaEngine, Pix2TextEngine
    from litforge.parsing.nemotron import NemotronEngine
    from litforge.parsing.vision import (
        GatewayVision,
        RoutedVision,
        Vision,
        enhance_project,
        page_engine,
        plan_project,
    )

    st = _state(ctx)
    conn = st.db()
    _, pid = _project(st, conn, project, required=True)
    assert pid is not None
    if only not in MARK_SCOPES:
        typer.secho(f"--only must be one of: {', '.join(MARK_SCOPES)}", fg="red", err=True)
        raise typer.Exit(2)
    if level not in ("medium", "high"):
        typer.secho("--level must be medium or high", fg="red", err=True)
        raise typer.Exit(2)
    try:
        engine: Vision | FormulaEngine
        if level == "medium":
            engine = Pix2TextEngine()
            typer.echo(f"Pix2Text on the {engine.device.upper()}")
        else:
            # Made only when a page needs it: Nemotron alone must not require a vision-model profile.
            def gateway() -> GatewayVision:
                return GatewayVision(ModelGateway(conn, st.config), profile, approve)

            if preview:
                chosen = page_engine(st.paths, st.config, gateway)
                model_engine = chosen.model if isinstance(chosen, RoutedVision) else chosen
                calls = plan_project(conn, st.paths, pid, model_engine, only,
                                     routed=chosen if isinstance(chosen, RoutedVision) else None
                                     ) if isinstance(model_engine, GatewayVision) else []
                typer.echo(json.dumps({"image_description": f"{len(calls)} page images",
                                       "calls": [call.model_dump() for call in calls]}, indent=1))
                return

            def model() -> Vision:
                made = gateway()
                made.check()
                return made

            engine = page_engine(st.paths, st.config, model)
            if isinstance(engine, RoutedVision):
                typer.echo(f"Nemotron Parse for English pages, {engine.model.name if engine.model else 'no model'}"
                           " for Persian pages")
            elif isinstance(engine, NemotronEngine):
                typer.echo("Nemotron Parse for every page")
        report = enhance_project(conn, st.paths, st.config, pid, engine, only, log=st.log, progress=typer.echo)
    except (ValueError, RuntimeError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{report.pages_improved} pages rewritten in {report.papers} papers"
               + (f"; {report.pages_rejected} kept as extracted (did not match the page)" if report.pages_rejected
                  else "") + ". Run `litforge export` to write the notes.")


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
    """Open the web interface in your browser (runs locally on Linux, macOS and Windows)."""
    from litforge.ui.server import serve

    st = _state(ctx)
    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(f"warning: serving on {host}; anyone who can reach this address can use the UI", fg="yellow")
    serve(st.paths, st.config, host, port, not no_browser)


@app.command()
def shortcut() -> None:
    """Add a LitForge icon to your apps that opens the web interface with one click."""
    import shutil
    import sys

    from litforge.shortcut import create_shortcut

    exe = shutil.which("litforge") or str(Path(sys.argv[0]).resolve())
    gui = shutil.which("litforge-app")
    icon = Path(__file__).parent / "ui" / "static" / "icon.svg"
    written = create_shortcut(sys.platform, Path.home(), Path.cwd(), Path(exe), icon, Path(gui) if gui else None)
    for path in written:
        typer.echo(f"created {path}")
    where = {"darwin": "Launchpad (drag it to the Dock to keep it there)",
             "win32": "the LitForge icon on your desktop"}.get(sys.platform, "Show Apps (pin it to the dock)")
    typer.echo(f"Open LitForge from {where}. Stop it with Quit in the page.")
    typer.echo(f"It uses the data in {Path.cwd()}, the folder you ran this command in.")


def launch() -> None:
    """Entry point of the windowless `litforge-app` launcher: the web interface with default options."""
    app(["ui"])

register(app, eval_app)
register_console(app)

if __name__ == "__main__":
    app()
