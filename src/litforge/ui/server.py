"""Local web UI: a small JSON API plus a single static page (Python standard library only).

Binds to 127.0.0.1 by default. Long operations (similar-paper search, keyword search, PDF downloads) run in a
background thread; the page polls /api/jobs/<id> for progress.
"""

from __future__ import annotations

import dataclasses
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from litforge.config import Config, EnvSettings, Paths, contact_email, save_mailto
from litforge.discovery.fetch import fetch_open_access
from litforge.discovery.http import HttpClient
from litforge.discovery.run import Sources, run_import, run_search
from litforge.discovery.similar import run_similar
from litforge.discovery.textsim import Embedder, load_embedder
from litforge.export.markdown import default_export_dir, export_markdown
from litforge.ingest import IngestResult
from litforge.observability import EventLog
from litforge.parsing.formulas import FormulaEngine, Pix2TextEngine, formulas_available
from litforge.parsing.run import ParseResult
from litforge.parsing.vision import OllamaVision, Vision, enhance_project
from litforge.pipeline import add_pdfs
from litforge.projects import ProjectNotFound, create_project, delete_project, get_project_id, set_active
from litforge.screening.criteria import load_criteria
from litforge.screening.llm import Llm, LlmError, ollama_from_config
from litforge.screening.run import screen_run
from litforge.storage.cache import sha256_file
from litforge.storage.db import open_db, transaction
from litforge.ui import api

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
STATIC = resources.files("litforge.ui") / "static"


@dataclass
class Job:
    id: str
    kind: str
    project: str
    status: str = "running"  # running | done | failed
    messages: list[str] = field(default_factory=list)
    run_id: str | None = None
    error: str | None = None
    report: dict[str, Any] | None = None  # structured result, for jobs whose page shows one


class App:
    """Shared state for request handlers."""

    def __init__(self, paths: Paths, config: Config, make_http: Callable[[], HttpClient] | None = None,
                 make_llm: Callable[[str | None], Llm] | None = None,
                 make_vision: Callable[[], Vision] | None = None,
                 make_formulas: Callable[[], FormulaEngine] | None = None) -> None:
        self.paths = paths
        self.config = config
        self.log = EventLog(paths.logs)
        self.jobs: dict[str, Job] = {}
        self._embedder: Embedder | None = None
        self._embedder_loaded = False
        self._lock = threading.Lock()
        self.make_http = make_http or (lambda: HttpClient(
            cache_dir=paths.http_cache, cache_ttl_s=config.discovery.cache_ttl_days * 86400))
        self.make_llm = make_llm or (lambda model: ollama_from_config(config.screening, model))
        self.make_vision = make_vision or (lambda: OllamaVision(
            os.environ.get("LITFORGE_OLLAMA_URL") or config.screening.ollama_url, config.vision))
        self._make_formulas = make_formulas or Pix2TextEngine
        self._formulas: FormulaEngine | None = None
        paths.ensure()
        conn = open_db(paths.db)  # applies migrations once at startup
        # Jobs live in memory: runs left 'running' by a previous server process will never finish.
        conn.execute("UPDATE discovery_runs SET status='interrupted' WHERE status='running'")
        conn.close()

    def formulas(self) -> FormulaEngine:
        """Pix2Text loads its models once and is reused across exports."""
        with self._lock:
            if self._formulas is None:
                self._formulas = self._make_formulas()
            return self._formulas

    def db(self) -> sqlite3.Connection:
        return open_db(self.paths.db)

    def sources(self) -> Sources:
        env = EnvSettings()
        return Sources(self.make_http(), env.openalex_api_key, contact_email(self.paths, self.config))

    def embedder(self, wanted: bool) -> Embedder | None:
        if not wanted or not self.config.similar.embedding_model:
            return None
        with self._lock:  # load the model once and reuse it across jobs
            if not self._embedder_loaded:
                cfg = self.config.similar
                self._embedder = load_embedder(cfg.embedding_model, self.paths.model_cache, cfg.embedding_device)
                self._embedder_loaded = True
        return self._embedder

    def ensure_project(self, conn: sqlite3.Connection, name: str, export_dir: str | None) -> str:
        name = name.strip()
        if not re.fullmatch(r"[\w][\w.\- ]{0,80}", name):
            raise ValueError("project name: letters, digits, space, '.', '-', '_' (max 80)")
        try:
            pid = get_project_id(conn, name)
        except ProjectNotFound:
            pid = create_project(conn, self.paths, name)
        if export_dir is not None:
            chosen = export_dir.strip()
            if chosen and Path(chosen).expanduser().resolve() == self.paths.default_exports(name):
                chosen = ""  # the default: keep following the project folder
            with transaction(conn):
                api.set_export_dir(conn, pid, chosen or None)
        set_active(self.paths, name)
        return pid

    def start_job(self, kind: str, project: str, work: Callable[[Job], str | None]) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind, project=project)
        self.jobs[job.id] = job

        def target() -> None:
            try:
                job.run_id = work(job)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - reported to the page
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
                self.log.emit("ui.job_failed", job=job.id, error=job.error, trace=traceback.format_exc())

        threading.Thread(target=target, daemon=True).start()
        return job

    # ---- jobs ----------------------------------------------------------------

    def similar_job(self, body: dict[str, Any]) -> Job:
        conn = self.db()
        project = str(body.get("project") or "default")
        pid = self.ensure_project(conn, project, body.get("export_dir"))
        seeds = [s.strip() for s in body.get("seeds", []) if str(s).strip()]
        if body.get("from_relevant"):
            seeds += api.relevant_seeds(conn, pid)
        if body.get("seed_run"):
            seeds += api.run_paper_ids(conn, str(body["seed_run"]), limit=int(body.get("seed_limit", 15)))
        conn.close()
        title = (body.get("title") or "").strip() or None
        abstract = (body.get("abstract") or "").strip() or None
        if not seeds and not title and not abstract:
            raise ValueError("give a DOI/arXiv id, a PDF, a title or an abstract")

        def work(job: Job) -> str | None:
            job.messages.append("loading embedding model" if body.get("embeddings", True) else "starting")
            embedder = self.embedder(bool(body.get("embeddings", True)))
            c = self.db()
            try:
                report = run_similar(c, self.paths, self.config, self.sources(), pid, seeds, title, abstract,
                                     embedder, top_n=0, log=self.log, progress=job.messages.append)
            finally:
                c.close()
            job.messages += [f"warning: {e}" for e in report.errors]
            job.messages.append(f"done: {report.papers} papers, {report.new_to_project} new")
            return report.run_id

        return self.start_job("similar", project, work)

    def search_job(self, body: dict[str, Any]) -> Job:
        conn = self.db()
        project = str(body.get("project") or "default")
        pid = self.ensure_project(conn, project, body.get("export_dir"))
        conn.close()
        queries = [q.strip() for q in body.get("queries", []) if str(q).strip()]
        if not queries:
            raise ValueError("give at least one search query")
        sources = body.get("sources") or self.config.discovery.sources

        def work(job: Job) -> str | None:
            c = self.db()
            try:
                report = run_search(c, self.sources(), pid, queries, sources, self.config.discovery.limit, top_n=0,
                                    progress=job.messages.append)
            finally:
                c.close()
            job.messages += [f"warning: {e}" for e in report.errors]
            job.messages.append(f"done: {report.papers} papers, {report.new_to_project} new")
            return report.run_id

        return self.start_job("search", project, work)

    def add_job(self, body: dict[str, Any]) -> Job:
        """Add PDFs from a folder on this computer and/or files uploaded from the page."""
        uploads = (self.paths.root / "uploads").resolve()
        files = [Path(f).resolve() for f in body.get("files") or []]
        if any(not f.is_relative_to(uploads) for f in files):
            raise ValueError("uploaded files must come from /api/upload")
        folder = str(body.get("folder") or "").strip()
        sources = list(files)
        if folder:
            path = Path(folder).expanduser()
            if not path.exists():
                raise ValueError(f"folder not found: {folder}")
            sources.append(path)
        if not sources:
            raise ValueError("give a folder or drop PDF files")
        conn = self.db()
        project = str(body.get("project") or "default")
        pid = self.ensure_project(conn, project, body.get("export_dir"))
        conn.close()
        label = Path(folder).expanduser().name if folder else f"{len(files)} uploaded PDF(s)"

        def work(job: Job) -> str | None:
            c = self.db()
            try:
                report, results, parsed = add_pdfs(c, self.paths, self.config, pid, sources, label, self.log,
                                                   progress=job.messages.append)
            finally:
                c.close()
                for f in files:  # the library keeps its own copy
                    f.unlink(missing_ok=True)
            job.messages += [f"warning: {e}" for e in report.errors]
            c = self.db()
            try:
                job.report = self._add_report(c, project, pid, results, parsed)
            finally:
                c.close()
            new = sum(r.status == "new" for r in results)
            known = sum(r.status == "duplicate" and r.paper_id is not None for r in results)
            full = sum(p.status != "failed" for p in parsed)
            job.messages.append(f"done: {len(results)} PDFs, {new} new, {known} already in the library; "
                                f"{full} with full text")
            return report.run_id

        return self.start_job("pdfs", project, work)

    def _add_report(self, conn: sqlite3.Connection, project: str, pid: str, results: list[IngestResult],
                    parsed: list[ParseResult]) -> dict[str, Any]:
        """What happened to each added file: attached to a paper the project was missing a PDF for, a new
        paper, already in the library, or rejected; and where the files are kept."""
        failed_parse = {p.artifact_id for p in parsed if p.status == "failed"}
        items = []
        for r in results:
            title = None
            if r.paper_id:
                row = conn.execute("SELECT title FROM papers WHERE id=?", (r.paper_id,)).fetchone()
                title = row["title"] if row else None
            if r.status == "new":
                outcome = "attached" if r.message == "attached to an existing library record" else "new_paper"
            elif r.status == "duplicate" and r.paper_id:
                outcome = "already"
            else:
                outcome = "failed"
            full_text = outcome != "failed" and r.artifact_id not in failed_parse and conn.execute(
                "SELECT 1 FROM parse_runs pr JOIN artifacts a ON a.id = pr.artifact_id WHERE a.paper_id=?"
                " AND pr.is_current = 1 AND pr.status != 'failed'", (r.paper_id,)).fetchone() is not None
            name = Path(r.path).name
            if Path(r.path).parent == self.paths.root / "uploads":  # stored as <hash>-<name> by /api/upload
                name = name.split("-", 1)[-1]
            items.append({"file": name, "outcome": outcome, "paper_id": r.paper_id, "title": title,
                          "full_text": full_text, "reason": r.message or r.status})
        return {"items": items, "library": str(self.paths.pdfs),
                "export_dir": str(default_export_dir(conn, self.paths, project, pid).expanduser().resolve())}

    def screen_job(self, body: dict[str, Any]) -> Job:
        """Screen the top papers of a run with the local model; reports back the run so the page reloads it."""
        run_id = str(body["run_id"])
        top = self.config.screening.default_top if body.get("top") is None else int(body["top"])
        if top < 1:
            raise ValueError("top must be at least 1")
        conn = self.db()
        try:
            run = conn.execute("SELECT r.project_id, p.name FROM discovery_runs r JOIN projects p"
                               " ON p.id = r.project_id WHERE r.id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("run not found")
            if load_criteria(conn, run["project_id"]).is_empty():
                raise ValueError("no screening criteria: fill in the research question first")
        finally:
            conn.close()
        model = str(body.get("model") or "") or None

        def work(job: Job) -> str | None:
            c = self.db()
            try:
                screen_run(c, self.make_llm(model), run_id, top, progress=job.messages.append, log=self.log)
            except LlmError as exc:
                raise RuntimeError(str(exc)) from exc
            finally:
                c.close()
            return run_id

        return self.start_job("screen", str(run["name"]), work)

    def export_job(self, body: dict[str, Any], export: bool = True) -> Job:
        """Export in the background: first download open-access PDFs of the chosen papers (`fetch`) and/or
        rewrite their hard pages with the vision model (`vision`), then export the same papers (with
        `export` False, only download). The job reports back the run being viewed so the page can refresh
        its PDF badges."""
        conn = self.db()
        project = str(body["project"])
        pid = self.ensure_project(conn, project, body.get("export_dir"))
        conn.close()
        paper_ids, only = body.get("paper_ids"), str(body.get("only", "all"))

        fetch = bool(body.get("fetch")) or not export
        level = str(body.get("math") or ("high" if body.get("vision") else "off"))
        if level not in ("off", "medium", "high"):
            raise ValueError("math must be off, medium or high")
        if level == "medium" and self._make_formulas is Pix2TextEngine and not formulas_available():
            raise ValueError("better math (medium) needs Pix2Text: run `uv sync --extra formulas-gpu` "
                             "(NVIDIA GPU) or `uv sync --extra formulas` (CPU), then restart `litforge ui`")
        engine: Vision | FormulaEngine | None = self.make_vision() if level == "high" else None
        if isinstance(engine, OllamaVision):
            engine.check()  # fail now, on the page, if Ollama or the model is missing

        def work(job: Job) -> str | None:
            c = self.db()
            job.report = {"library": str(self.paths.pdfs),
                          "export_dir": str(default_export_dir(c, self.paths, project, pid).expanduser().resolve())}
            try:
                if fetch:
                    self._fetch(c, job, pid, only, paper_ids)
                if level != "off":
                    tool = engine if engine is not None else self.formulas()
                    vis = enhance_project(c, self.paths, self.config, pid, tool, only, paper_ids, self.log,
                                          progress=job.messages.append)
                    job.report["vision"] = {**dataclasses.asdict(vis), "level": level}
                    job.messages.append(f"better math ({level}): {vis.pages_improved} pages rewritten in "
                                        f"{vis.papers} papers" + (f", {vis.pages_rejected} kept as extracted"
                                                                 if vis.pages_rejected else ""))
                if export:
                    out, count = export_markdown(c, self.paths, project, pid, only=only, paper_ids=paper_ids)
                    job.messages.append(f"done: exported {count} papers to {out}")
            finally:
                c.close()
            return body.get("run_id")

        return self.start_job("export" if export else "fetch", project, work)

    def _fetch(self, c: sqlite3.Connection, job: Job, pid: str, only: str, paper_ids: list[str] | None) -> None:
        """Download open-access PDFs of the chosen papers; adds one line per paper to the job's report."""
        report = fetch_open_access(c, self.paths, self.config, self.make_http(), pid, only, paper_ids,
                                   self.log, progress=job.messages.append,
                                   mailto=contact_email(self.paths, self.config))
        job.messages += [f"warning: {e}" for e in report.failed]
        job.messages.append(
            f"{report.downloaded} of {report.candidates} missing PDFs downloaded, {report.parsed} with full text"
            + (f"; {report.no_source} have no open-access copy" if report.no_source else ""))
        assert job.report is not None
        job.report.update(downloaded=report.downloaded, candidates=report.candidates, items=report.items)


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LitForge"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            pass  # keep the terminal quiet; errors go to the JSONL log

        # ---- helpers ----

        def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _screen_info(self) -> dict[str, Any]:
            return {"model": app.config.screening.model, "default_top": app.config.screening.default_top}

        def _same_origin(self) -> bool:
            """Reject cross-site requests that would otherwise reach a localhost API."""
            origin = self.headers.get("Origin")
            return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

        # ---- routing ----

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                self._get(url.path, query)
            except LookupError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
            except (ValueError, ProjectNotFound) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))

        def _get(self, path: str, query: dict[str, str]) -> None:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            conn = app.db()
            try:
                if path == "/api/info":
                    return self._json({"exports_root": str(app.paths.exports_root),
                                       "math": {"medium": formulas_available(), "high": app.config.vision.model}})
                if path == "/api/projects":
                    return self._json(api.list_projects(conn, app.paths))
                if path == "/api/runs":
                    pid = get_project_id(conn, query["project"]) if query.get("project") else None
                    return self._json(api.list_runs(conn, pid, int(query.get("limit", 50))))
                if path == "/api/library":
                    return self._json(api.library(conn))
                if path == "/api/settings":
                    return self._json({"mailto": contact_email(app.paths, app.config)})
                if path == "/api/missing-pdfs":
                    pid = get_project_id(conn, query["project"])
                    return self._json(api.missing_pdfs(conn, pid, query.get("only", "all")))
                if path == "/api/criteria":
                    return self._json({**api.get_criteria(conn, query["project"]), **self._screen_info()})
                if m := re.fullmatch(r"/api/runs/([\w-]+)", path):
                    return self._json(api.run_results(conn, m.group(1)))
                if m := re.fullmatch(r"/api/jobs/(\w+)", path):
                    job = app.jobs.get(m.group(1))
                    if job is None:
                        raise LookupError("job not found")
                    return self._json(job.__dict__)
                if m := re.fullmatch(r"/api/papers/([\w-]+)", path):
                    project_id = get_project_id(conn, query["project"]) if query.get("project") else None
                    return self._json(api.paper_detail(conn, app.paths, m.group(1), project_id))
                if m := re.fullmatch(r"/api/papers/([\w-]+)/pdf", path):
                    pdf, name = api.paper_pdf(conn, app.paths, m.group(1))
                    return self._send(200, pdf.read_bytes(), "application/pdf",
                                      {"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
                if m := re.fullmatch(r"/api/papers/([\w-]+)/files/(.+)", path):
                    target = api.paper_file(conn, app.paths, m.group(1), unquote(m.group(2)))
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self._send(200, target.read_bytes(), ctype)
                raise LookupError("not found")
            finally:
                conn.close()

        def _static(self, rel: str) -> None:
            target = STATIC.joinpath(*[p for p in rel.split("/") if p not in ("", ".", "..")])
            if not target.is_file():
                raise LookupError("not found")
            ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            if not self._same_origin():
                return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            url = urlsplit(self.path)
            try:
                if url.path == "/api/upload":
                    return self._upload(parse_qs(url.query).get("name", ["paper.pdf"])[0])
                if url.path == "/api/import":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    return self._import(q.get("name", "refs.csv"), q.get("project") or "default", q.get("export_dir"))
                body = self._body()
                if url.path == "/api/similar":
                    return self._json(app.similar_job(body).__dict__)
                if url.path == "/api/search":
                    return self._json(app.search_job(body).__dict__)
                if url.path == "/api/add":
                    return self._json(app.add_job(body).__dict__)
                if url.path == "/api/screen":
                    return self._json(app.screen_job(body).__dict__)
                if url.path == "/api/criteria":
                    conn = app.db()
                    try:
                        with transaction(conn):
                            saved = api.put_criteria(conn, str(body["project"]), body)
                    finally:
                        conn.close()
                    return self._json({**saved, **self._screen_info()})
                if url.path == "/api/export-dir":  # "" = back to data/projects/<name>/exports
                    conn = app.db()
                    try:
                        app.ensure_project(conn, body["project"], str(body.get("export_dir") or ""))
                    finally:
                        conn.close()
                    return self._json({"ok": True})
                if url.path == "/api/open-exports":  # show the export folder in Finder / the file manager
                    conn = app.db()
                    try:
                        name = str(body["project"])
                        folder = default_export_dir(conn, app.paths, name, get_project_id(conn, name))
                    finally:
                        conn.close()
                    folder.mkdir(parents=True, exist_ok=True)
                    if sys.platform == "win32":
                        os.startfile(folder)  # type: ignore[attr-defined]  # noqa: S606
                    else:
                        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])  # noqa: S603
                    return self._json({"path": str(folder)})
                if url.path == "/api/delete-project":
                    name = str(body["project"])
                    if any(j.project == name and j.status == "running" for j in app.jobs.values()):
                        raise ValueError("a job is still running in this project")
                    conn = app.db()
                    try:
                        deleted = delete_project(conn, app.paths, name, bool(body.get("delete_exports")))
                    finally:
                        conn.close()
                    return self._json(deleted)
                if url.path == "/api/delete-run":
                    conn = app.db()
                    try:
                        with transaction(conn):
                            res = api.delete_run(conn, str(body["run_id"]))
                    finally:
                        conn.close()
                    return self._json(res)
                if url.path == "/api/marks":
                    conn = app.db()
                    with transaction(conn):
                        ids = body["paper_ids"] if "paper_ids" in body else [body["paper_id"]]
                        api.set_marks(conn, body["project"], ids, body.get("mark"))
                    conn.close()
                    return self._json({"ok": True})
                if url.path == "/api/settings":
                    save_mailto(app.paths, body.get("mailto"))
                    return self._json({"mailto": contact_email(app.paths, app.config)})
                if url.path == "/api/fetch":
                    return self._json(app.export_job(body, export=False).__dict__)
                if url.path == "/api/export" and (body.get("fetch") or body.get("vision") or body.get("math")):
                    return self._json(app.export_job(body).__dict__)
                if url.path == "/api/export":
                    conn = app.db()
                    try:
                        pid = app.ensure_project(conn, body["project"], body.get("export_dir"))
                        out, count = export_markdown(conn, app.paths, body["project"], pid,
                                                     only=body.get("only", "all"), paper_ids=body.get("paper_ids"))
                    finally:
                        conn.close()
                    return self._json({"path": str(out), "papers": count})
                self._error(HTTPStatus.NOT_FOUND, "not found")
            except (ValueError, KeyError, ProjectNotFound, json.JSONDecodeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))

        def _upload(self, name: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_UPLOAD_BYTES:
                return self._error(HTTPStatus.BAD_REQUEST, "empty or too large upload")
            data = self.rfile.read(length)
            if not data.startswith(b"%PDF-"):
                return self._error(HTTPStatus.BAD_REQUEST, "not a PDF")
            uploads = app.paths.root / "uploads"
            uploads.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.\-]+", "_", Path(name).name)[:100] or "paper.pdf"
            tmp = uploads / f".{uuid.uuid4().hex}.part"
            tmp.write_bytes(data)
            dest = uploads / f"{sha256_file(tmp)[:12]}-{safe}"
            tmp.replace(dest)
            self._json({"path": str(dest), "name": safe})

        def _import(self, name: str, project: str, export_dir: str | None) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 20 * 1024 * 1024:
                return self._error(HTTPStatus.BAD_REQUEST, "empty or too large file")
            safe = re.sub(r"[^\w.\- ]+", "_", Path(name).name)[:120]
            if Path(safe).suffix.lower() not in (".bib", ".ris", ".csv"):
                return self._error(HTTPStatus.BAD_REQUEST, "use a .bib, .ris or .csv file")
            uploads = app.paths.root / "uploads" / uuid.uuid4().hex[:8]
            uploads.mkdir(parents=True, exist_ok=True)
            target = uploads / safe
            target.write_bytes(self.rfile.read(length))
            conn = app.db()
            try:
                pid = app.ensure_project(conn, project, export_dir)
                report = run_import(conn, pid, target, top_n=0)
            finally:
                conn.close()
            if report.status == "failed":
                return self._error(HTTPStatus.BAD_REQUEST, "; ".join(report.errors))
            self._json({"run_id": report.run_id, "papers": report.papers, "created": report.created})

    return Handler


def serve(paths: Paths, config: Config, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> None:
    import webbrowser

    # A second click on the shortcut: show the LitForge that is already running. Checked before binding,
    # since on Windows a second server could bind the same port without an error.
    if port and _is_litforge(f"http://{host}:{port}/"):
        print(f"LitForge is already running on http://{host}:{port}/")
        if open_browser:
            webbrowser.open(f"http://{host}:{port}/")
        return
    app = App(paths, config)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"LitForge UI on {url}  (close this window or press Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _is_litforge(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url + "api/info", timeout=2) as r:  # noqa: S310 (local address)
            return "exports_root" in json.loads(r.read())
    except (OSError, ValueError):
        return False
