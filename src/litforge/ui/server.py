"""Local web UI: a small JSON API plus a single static page (Python standard library only).

Binds to 127.0.0.1 by default. Long operations (similar-paper search, keyword search) run in a
background thread; the page polls /api/jobs/<id> for progress.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
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
from urllib.parse import parse_qs, unquote, urlsplit

from litforge.config import Config, EnvSettings, Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.run import Sources, run_import, run_search
from litforge.discovery.similar import run_similar
from litforge.discovery.textsim import Embedder, load_embedder
from litforge.export.markdown import export_markdown
from litforge.observability import EventLog
from litforge.projects import ProjectNotFound, create_project, get_project_id, set_active
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


class App:
    """Shared state for request handlers."""

    def __init__(self, paths: Paths, config: Config, make_http: Callable[[], HttpClient] | None = None) -> None:
        self.paths = paths
        self.config = config
        self.log = EventLog(paths.logs)
        self.jobs: dict[str, Job] = {}
        self._embedder: Embedder | None = None
        self._embedder_loaded = False
        self._lock = threading.Lock()
        self.make_http = make_http or (lambda: HttpClient(
            cache_dir=paths.http_cache, cache_ttl_s=config.discovery.cache_ttl_days * 86400))
        paths.ensure()
        conn = open_db(paths.db)  # applies migrations once at startup
        # Jobs live in memory: runs left 'running' by a previous server process will never finish.
        conn.execute("UPDATE discovery_runs SET status='interrupted' WHERE status='running'")
        conn.close()

    def db(self) -> sqlite3.Connection:
        return open_db(self.paths.db)

    def sources(self) -> Sources:
        env = EnvSettings()
        return Sources(self.make_http(), env.openalex_api_key, env.mailto or self.config.discovery.mailto)

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
            with transaction(conn):
                api.set_export_dir(conn, pid, export_dir.strip() or None)
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


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LitForge"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            pass  # keep the terminal quiet; errors go to the JSONL log

        # ---- helpers ----

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
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
                if path == "/api/projects":
                    return self._json(api.list_projects(conn, app.paths))
                if path == "/api/runs":
                    return self._json(api.list_runs(conn, get_project_id(conn, query["project"])))
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
                if url.path == "/api/marks":
                    conn = app.db()
                    with transaction(conn):
                        api.set_mark(conn, body["project"], body["paper_id"], body.get("mark"))
                    conn.close()
                    return self._json({"ok": True})
                if url.path == "/api/export":
                    conn = app.db()
                    try:
                        pid = app.ensure_project(conn, body["project"], body.get("export_dir"))
                        out = export_markdown(conn, app.paths, body["project"], pid)
                    finally:
                        conn.close()
                    return self._json({"path": str(out)})
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
    app = App(paths, config)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"LitForge UI on {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
