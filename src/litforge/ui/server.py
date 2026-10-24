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
import time
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

import yaml
from pydantic import ValidationError

from litforge import desktop
from litforge.analysis import ask as answers
from litforge.analysis import audit as audits
from litforge.analysis import claim_graph, claims, figures, gaps, living, repro, snapshots
from litforge.analysis import matrix as matrices
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.review import record_review
from litforge.analysis.runs import get_run
from litforge.config import (
    Config,
    EnvSettings,
    Paths,
    contact_email,
    load_config,
    load_settings,
    save_mailto,
    save_update_check,
)
from litforge.console import planner
from litforge.console.actions import ActionContext
from litforge.discovery.fetch import fetch_open_access
from litforge.discovery.http import HttpClient
from litforge.discovery.links import run_links
from litforge.discovery.run import Sources, run_import, run_search
from litforge.discovery.similar import run_similar
from litforge.discovery.textsim import Embedder, load_embedder
from litforge.discovery.versions import (
    confirm_version_link,
    link_project_versions,
    reject_version_link,
    set_primary,
    unlink_version,
)
from litforge.export.markdown import default_export_dir, export_markdown
from litforge.ingest import IngestResult
from litforge.jobs import JobCancelled, JobControl, check_cancelled, job_scope
from litforge.llm.local_models import fit_check, ollama_change, ollama_pull
from litforge.llm.model_setup import config_change, math_high_cloud
from litforge.llm.secrets import KeyStore
from litforge.observability import EventLog
from litforge.parsing import nemotron
from litforge.parsing.formulas import FormulaEngine, Pix2TextEngine, formulas_available
from litforge.parsing.markitdown_parser import is_document
from litforge.parsing.mineru_parser import find_command, install_mineru, mineru_status
from litforge.parsing.run import ParseResult, parse_artifact
from litforge.parsing.vision import (
    GatewayVision,
    OllamaVision,
    RoutedVision,
    Vision,
    enhance_project,
    page_engine,
    plan_project,
)
from litforge.parsing.vision_setup import OllamaSetup
from litforge.pipeline import add_pdfs
from litforge.projects import MARK_SCOPES, ProjectNotFound, create_project, delete_project, get_project_id, set_active
from litforge.screening import active, fulltext
from litforge.screening.criteria import load_criteria
from litforge.screening.llm import Llm, LlmError, ollama_from_config
from litforge.screening.run import screen_run
from litforge.storage.cache import sha256_file
from litforge.storage.db import open_db, transaction
from litforge.storage.recovery import repair_abandoned_runs
from litforge.tools import (
    install_nemotron,
    install_pix2text,
    install_timeout_s,
    nemotron_install_status,
    pix2text_install_status,
)
from litforge.ui import api, review_routes, writing_routes
from litforge.updates import UpdateChecker, automatic_enabled
from litforge.writing import documents as wd
from litforge.writing.compile import engine_status

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
class BodyTooLarge(ValueError):
    pass


MAX_JSON_BODY = 16 * 1024 * 1024  # an 8 MB image as base64 in the Write view's AI request, with room to spare
HEARTBEAT_S = 15  # how often an open page checks in (app.js)
IDLE_STOP_S = 120  # no check-in for this long, and no job running: the server stops itself
STATIC = resources.files("litforge.ui") / "static"
mimetypes.add_type("text/javascript", ".mjs")  # PDF.js modules; the Windows registry may say otherwise


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


class _Messages(list[str]):
    """A job's progress log; adding a line is where a job the user stopped stops."""

    def __init__(self, check: Callable[[], None]) -> None:
        super().__init__()
        self._check = check

    def append(self, line: str) -> None:
        self._check()
        super().append(line)


class App:
    """Shared state for request handlers."""

    def __init__(self, paths: Paths, config: Config, make_http: Callable[[], HttpClient] | None = None,
                 make_llm: Callable[[str | None], Llm] | None = None,
                 make_vision: Callable[[], Vision] | None = None,
                 make_formulas: Callable[[], FormulaEngine] | None = None,
                 vision_setup: OllamaSetup | None = None,
                 make_gateway: Callable[[sqlite3.Connection], ModelGateway] | None = None) -> None:
        self.paths = paths
        self.updates = UpdateChecker(paths)
        self.config = config
        self.models = api.ModelsPanel(paths, config)
        self.keys = KeyStore(config)
        self._job_context = threading.local()
        self.log = EventLog(paths.logs)
        self.jobs: dict[str, Job] = {}
        self._workers: dict[str, tuple[threading.Thread, JobControl]] = {}
        self._workers_lock = threading.Lock()
        self._closing = threading.Event()
        self.model_downloads: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
        self.stop: Callable[[], None] | None = None  # set by serve(): shuts the server down
        self.last_seen = time.monotonic()  # when a page last checked in, see stop_when_idle()
        self.desktop = False  # served to the desktop window (litforge desktop), not to a browser tab
        self._embedder: Embedder | None = None
        self._embedder_loaded = False
        self._lock = threading.Lock()
        self.make_http = make_http or (lambda: HttpClient(
            cache_dir=paths.http_cache, cache_ttl_s=config.discovery.cache_ttl_days * 86400))
        self.make_llm = make_llm or (lambda model: ollama_from_config(self.config.screening, model))
        self.make_gateway = make_gateway or (lambda conn: ModelGateway(
            conn, self.config, ensure_server=self.models.server.ensure_running,
            progress=getattr(self._job_context, "progress", None)))
        self.make_vision = make_vision
        self.vision_setup = vision_setup or OllamaSetup(
            os.environ.get("LITFORGE_OLLAMA_URL") or config.screening.ollama_url, config.vision.model or "disabled")
        self._make_formulas = make_formulas or Pix2TextEngine
        self._formulas: FormulaEngine | None = None
        paths.ensure()
        conn = open_db(paths.db)  # applies migrations once at startup
        # Jobs live in memory: runs left 'running' by a previous server process will never finish.
        conn.execute("UPDATE discovery_runs SET status='interrupted' WHERE status='running'")
        repair_abandoned_runs(conn)
        conn.close()

    def mineru_install_job(self, body: dict[str, Any]) -> Job:
        if body.get('confirmed') is not True:
            raise ValueError('Confirm the MinerU download size first')
        if any(j.kind == 'mineru-install' and j.status == 'running' for j in self.jobs.values()):
            raise ValueError('MinerU installation is already running')
        def work(job: Job) -> None:
            install_mineru(self.paths, job.messages.append,
                           timeout_s=self.config.parsing.mineru.install_timeout_s)
            job.report = mineru_status(self.paths, self.config.parsing)
        return self.start_job('mineru-install', '', work)

    def pix2text_install_job(self, body: dict[str, Any]) -> Job:
        if body.get("confirmed") is not True:
            raise ValueError("Confirm the Pix2Text download size first")
        status = pix2text_install_status()
        if not status["offerable"]:
            raise ValueError("Pix2Text installation is unavailable here; run `" + status["command"] + "` manually")
        if any(j.kind == "pix2text-install" and j.status == "running" for j in self.jobs.values()):
            raise ValueError("Pix2Text installation is already running")

        def work(job: Job) -> None:
            job.messages.append(f"Installing Pix2Text ({status['size']})…")

            def progress(message: str) -> None:
                if len(job.messages) < 200:
                    job.messages.append(message[:500])
                elif len(job.messages) == 200:
                    job.messages.append("Further installer output is omitted.")

            result = install_pix2text(progress, timeout_s=install_timeout_s())
            job.report = result
            if formulas_available():
                job.messages.append("Pix2Text is ready for Math: medium.")
            else:
                job.messages.append("Installation finished. Restart LitForge to use Pix2Text.")

        return self.start_job("pix2text-install", "", work)

    def nemotron_install_job(self, body: dict[str, Any]) -> Job:
        if body.get("confirmed") is not True:
            raise ValueError("Confirm the Nemotron Parse download size first")
        status = nemotron_install_status(self.paths)
        if not status["offerable"]:
            raise ValueError("Nemotron Parse cannot be set up here: " + (status["reason"] or "it is already set up"))
        if any(j.kind in ("nemotron-install", "setup") and j.status == "running" for j in self.jobs.values()):
            raise ValueError("Nemotron Parse is already being set up")

        def work(job: Job) -> None:
            job.report = install_nemotron(self.paths, self._job_progress(job))
            job.messages.append("Nemotron Parse is ready for Math: high and broken PDFs.")

        return self.start_job("nemotron-install", "", work)

    @staticmethod
    def _job_progress(job: Job) -> Callable[[str], None]:
        def progress(message: str) -> None:
            if len(job.messages) < 200:
                job.messages.append(message[:500])
            elif len(job.messages) == 200:
                job.messages.append("Further installer output is omitted.")

        return progress

    def save_engines(self, body: dict[str, Any]) -> dict[str, str]:
        """Save the manual engine choices (Settings) to <data>/config.yaml and use them from now on."""
        parse = body.get("parse", self.config.parsing.complex_fallback)
        page = body.get("page", self.config.vision.engine)
        if parse not in ("auto", "nemotron", "mineru", "docling", "none"):
            raise ValueError("parse must be auto, nemotron, mineru, docling or none")
        if page not in ("auto", "nemotron", "vision_model"):
            raise ValueError("page must be auto, nemotron or vision_model")
        if self.config.config_path is not None:  # an explicit --config/LITFORGE_CONFIG file wins over Settings
            override = yaml.safe_load(Path(self.config.config_path).read_text(encoding="utf-8")) or {}
            pinned = [f"{a}.{b}" for a, b in (("parsing", "complex_fallback"), ("vision", "engine"))
                      if b in (override.get(a) or {})]
            if pinned:
                raise ValueError(f"{', '.join(pinned)} is set in {self.config.config_path}, which overrides "
                                 "Settings; change it there")
        config_change(self.paths.root, {"parsing": {"complex_fallback": parse}, "vision": {"engine": page}}).apply()
        self.config.parsing.complex_fallback, self.config.vision.engine = parse, page
        return {"parse": parse, "page": page}

    def _nemotron_serves(self) -> bool:
        """Will Math: high use Nemotron Parse (so a missing vision model does not stop it)?"""
        mode = self.config.vision.engine
        return (mode != "vision_model" and nemotron.available(self.paths)
                and (mode == "nemotron" or nemotron.gpu_ready()))

    def optional_tools_status(self) -> dict[str, Any]:
        mineru = mineru_status(self.paths, self.config.parsing)
        pix2text = pix2text_install_status()
        pix2text.pop("root", None)
        nemo = nemotron_install_status(self.paths)
        nemo.pop("root", None)
        vision = self.math_setup_status()
        return {
            "mineru": {**mineru, "size": "~4 GB", "available": True},
            "pix2text": pix2text,
            "nemotron": nemo,
            "engines": {"parse": self.config.parsing.complex_fallback, "page": self.config.vision.engine},
            "tectonic": engine_status(self.paths),
            "vision": {**vision, "legacy": not self._uses_model_settings(), "size": "model-dependent"},
            "desktop": desktop.desktop_status(),
        }

    def desktop_check_job(self) -> Job:
        """Settings → LitForge window → Check again: detect and prepare the window engine (next launch uses it)."""
        if any(j.kind == "desktop-setup" and j.status == "running" for j in self.jobs.values()):
            raise ValueError("the window engine is already being checked")

        def work(job: Job) -> None:
            job.report = dataclasses.asdict(desktop.configure_engine("auto"))

        return self.start_job("desktop-setup", "", work)

    def mineru_parse_job(self, paper_id: str) -> Job:
        if not find_command(self.paths, self.config.parsing):
            raise ValueError('MinerU is not installed; open Settings to install it')
        if any(j.kind == 'mineru-parse' and j.status == 'running' for j in self.jobs.values()):
            raise ValueError('A MinerU parse is already running')
        conn = self.db()
        try:
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE paper_id=? AND status='ok' ORDER BY created_at LIMIT 1",
                (paper_id,),
            ).fetchone()
            if artifact is None or not (self.paths.root / artifact['pdf_path']).is_file():
                raise LookupError('Paper has no PDF')
            if not str(artifact['pdf_path']).lower().endswith('.pdf'):
                raise ValueError('MinerU reads PDFs; this paper is a document, already read as text')
        finally:
            conn.close()
        def work(job: Job) -> None:
            conn = self.db()
            try:
                minutes = api.mineru_estimate_minutes(artifact['page_count'], api.mineru_seconds_per_page(conn))
                job.messages.append(f'Re-reading with MinerU (about {minutes} min; '
                                    'first use downloads about 2 GB of models)…')
                result = parse_artifact(conn, self.paths, self.config, artifact, force=True, parser='mineru')
                job.report = result.model_dump()
                if result.status != 'ok':
                    raise RuntimeError('Previous parse kept: ' + '; '.join(result.route_reasons))
                job.messages.append('Finished MinerU parse and re-indexed paper.')
            finally:
                conn.close()
        return self.start_job('mineru-parse', '', work)

    def stop_models(self) -> None:
        cfg = self.config.models.servers.get("llamacpp")
        if cfg:
            self.models.server.stop(cfg)

    def model_test_job(self, body: dict[str, Any]) -> Job:
        from litforge.llm.commands import diagnostic_call, key_status
        profile = str(body["profile"])
        vision, approved = body.get("vision") is True, body.get("approved") is True
        conn = self.db()
        try:
            _, cfg = self.make_gateway(conn).resolve("asset_analyze" if vision else "ask", profile, vision)
            if cfg.remote and (not approved or not self.config.privacy.allow_cloud):
                raise ValueError("Cloud tests require cloud permission and separate approval of test prompts/images.")
        finally:
            conn.close()
        def work(job: Job) -> None:
            conn = self.db()
            try:
                gateway = self.make_gateway(conn)
                _, cfg = gateway.resolve("asset_analyze" if vision else "ask", profile, vision)
                job.messages.append(f"key: {key_status(cfg.api_key_env)}")
                job.report = diagnostic_call(gateway, profile, vision, approved)
            finally:
                conn.close()
        return self.start_job("model-test", "", work)

    def download_job(self, body: dict[str, Any], *, plan: dict[str, Any] | None = None) -> Job:
        if body.get("confirmed") is not True:
            raise ValueError("Confirm the download and reviewed file changes first")
        if plan is None:
            plan = self.models.download_pending.get(str(body.get("token", "")))
        if plan is None:
            raise ValueError("Download preview expired; review it again")
        if plan["fit"]["badge"] == "too large" and body.get("too_large_confirmed") is not True:
            raise ValueError("Explicit confirmation required: model is too large for this machine")
        self.models.download_pending.pop(str(body.get("token", "")), None)
        state = {"action": ""}
        selected_plan = plan
        def work(job: Job) -> None:
            job.report = {"state": "downloading"}
            for item in [selected_plan["item"], *([selected_plan["mmproj"]] if selected_plan["mmproj"] else [])]:
                def progress(info: dict[str, Any], filename: str = item["path"]) -> None:
                    job.report = {"state": "downloading", "file": filename, **info}
                path = self.models.downloader.run(selected_plan["repo"], item, progress=progress,
                    control=lambda: "cancel" if self._closing.is_set() else state["action"])
                if path is None:
                    job.report = {"state": "paused" if state["action"] == "pause" else "cancelled"}
                    return
            if state["action"]:
                job.report = {"state": "paused" if state["action"] == "pause" else "cancelled"}
                return
            check_cancelled()
            selected_plan["change"].apply()
            self.models.config = load_config(self.config.config_path, data_root=self.paths.root)
            self.config = self.models.config
            job.report = {"state": "registered", "verified": True}
        job = self.start_job("model-download", "", work)
        self.model_downloads[job.id] = (selected_plan, state)
        return job

    def download_control(self, body: dict[str, Any]) -> Job:
        identifier, action = str(body.get("id", "")), body.get("action")
        if identifier not in self.model_downloads or action not in {"pause", "cancel", "resume"}:
            raise ValueError("Unknown model download/action")
        plan, state = self.model_downloads[identifier]
        job = self.jobs[identifier]
        if action == "resume":
            if job.status == "running" or (job.status != "failed" and (not job.report or
                                                                       job.report.get("state") != "paused")):
                raise ValueError("Wait for the download to pause before resuming")
            return self.download_job({"confirmed": True, "too_large_confirmed": True}, plan=plan)
        state["action"] = action
        if action == "cancel" and job.status != "running":
            for item in [plan["item"], *([plan["mmproj"]] if plan["mmproj"] else [])]:
                target = self.models.downloader.path(plan["repo"], item["path"])
                target.with_suffix(target.suffix + ".part").unlink(missing_ok=True)
            job.report = {"state": "cancelled"}
        return job

    def ollama_pull_job(self, body: dict[str, Any]) -> Job:
        if body.get("confirmed") is not True:
            raise ValueError("Confirm the Ollama download first")
        name = str(body.get("name", ""))
        change = ollama_change(self.paths.root, name, tasks=body.get("tasks") or [])
        def work(job: Job) -> None:
            ollama_pull(name, base_url=self.config.screening.ollama_url,
                        progress=lambda item: setattr(job, "report", item))
            # The user reviews registration separately; public weights download needs no API key.
            token = uuid.uuid4().hex
            self.models.pending[token] = change
            job.report = {"state": "pulled", "token": token, "diff": change.diff}
        return self.start_job("ollama-pull", "", work)

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
        control = JobControl()
        job = Job(id=uuid.uuid4().hex, kind=kind, project=project, messages=_Messages(control.check))

        def progress(message: str) -> None:
            control.check()
            job.messages.append(message)

        def target() -> None:
            self._job_context.progress = progress
            try:
                with job_scope(control):
                    job.run_id = work(job)
                    control.check()
                job.status = "done"
            except JobCancelled as exc:
                job.status, job.error = "failed", str(exc)
            except Exception as exc:  # noqa: BLE001 - reported to the page
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
                self.log.emit("ui.job_failed", job=job.id, error=job.error, trace=traceback.format_exc())

        worker = threading.Thread(target=target, daemon=True, name=f"ui-{kind}-{job.id}")
        with self._workers_lock:
            if self._closing.is_set():
                raise ValueError("server is shutting down")
            self.jobs[job.id] = job
            self._workers[job.id] = worker, control
            worker.start()
        return job

    def stop_job(self, job_id: str) -> Job:
        """Stop a running job at its next progress line (or paper); its external processes get SIGTERM,
        then SIGKILL two seconds later. Work already finished stays."""
        with self._workers_lock:
            job, worker = self.jobs.get(job_id), self._workers.get(job_id)
        if job is None:
            raise LookupError("job not found")
        if job.status != "running" or worker is None:
            return job
        control = worker[1]
        control.reason = "stopped"
        control.cancelled.set()
        stoppers = control.process_stoppers()
        for stop in stoppers:
            stop(False)
        if stoppers:
            timer = threading.Timer(2, lambda: [stop(True) for stop in stoppers])
            timer.daemon = True
            timer.start()
        return job

    def shutdown(self, timeout: float = 5) -> None:
        """Cancel all jobs, terminate owned process trees, then wait before stopping models."""
        deadline = time.monotonic() + max(0, timeout)
        with self._workers_lock:
            self._closing.set()
            workers = list(self._workers.values())
            for _, control in workers:
                control.cancelled.set()
        stoppers = [stop for _, control in workers for stop in control.process_stoppers()]
        for stop in stoppers:
            stop(False)
        # All groups share one grace period, including groups whose leader exits on SIGTERM.
        grace = min(deadline, time.monotonic() + 1)
        for worker, _ in workers:
            worker.join(max(0, grace - time.monotonic()))
        for stop in stoppers:
            stop(True)
        for worker, _ in workers:
            worker.join(max(0, deadline - time.monotonic()))
        self.stop_models()

    # ---- jobs ----------------------------------------------------------------

    def lens_job(self, request: api.LensRequest) -> Job:
        conn = self.db()
        try:
            if request.task in ('ask', 'claim_extract', 'claim_relation', 'screen_fulltext'):
                project = request.project or ''
            else:
                pid = (get_run(conn, request.run_id).project_id if request.task == 'audit'
                       else matrices.get_matrix(conn, request.matrix_id).project_id)
                row = conn.execute('SELECT name FROM projects WHERE id=?', (pid,)).fetchone()
                project = row['name'] if row else ''
        finally:
            conn.close()

        def work(job: Job) -> str | None:
            conn = self.db()
            try:
                gateway = self.make_gateway(conn)
                # Recheck the plan in the worker in case the library or budget changed.
                plan = api.lens_plan(conn, gateway, request)
                if plan['blocked_reasons'] or (plan['requires_approval'] and not request.approved):
                    raise ValueError('analysis plan needs approval or is blocked')
                job.messages.append('Working with library evidence…')
                if request.task == 'ask':
                    pid = get_project_id(conn, request.project) if request.project else None
                    answer = answers.ask(conn, gateway, request.question, pid, request.k, request.profile,
                                         request.approved, request.sensitive, all_versions=request.all_versions)
                    job.report = api.lens_answer(conn, answer.run_id)
                    return answer.run_id
                if request.task == 'audit':
                    audits.audit_answer(conn, gateway, request.run_id, request.profile, request.approved)
                    job.report = api.lens_answer(conn, request.run_id)
                    return request.run_id
                if request.task in ("claim_extract", "claim_relation", "screen_fulltext"):
                    if not request.project:
                        raise ValueError("choose a project")
                    pid = get_project_id(conn, request.project)
                    phase2: claims.ClaimsResult | claim_graph.RelationsResult | fulltext.FulltextReport
                    if request.task == "claim_extract":
                        phase2 = claims.extract_claims(conn, gateway, pid, request.paper_ids, request.profile,
                                                       request.approved, request.force, request.all_versions)
                    elif request.task == "claim_relation":
                        phase2 = claim_graph.classify_relations(conn, gateway, pid, request.max_pairs, request.profile,
                                                               request.approved, request.force)
                    else:
                        phase2 = fulltext.screen_fulltext(conn, gateway, pid, request.top, request.profile,
                                                        request.approved, request.force, request.paper_ids,
                                                        request.all_versions, only=request.only,
                                                        band=self.config.screening.borderline)
                    job.report = phase2.model_dump()
                    return phase2.run_id
                result = matrices.extract_matrix(conn, gateway, request.matrix_id, request.paper_ids,
                                                  request.profile, request.approved, request.force,
                                                  request.all_versions)
                job.report = api.lens_matrix(conn, request.matrix_id, request.all_versions)
                job.report['stats'] = {
                    'extracted': sum(c.run_id == result.run_id for c in result.cells),
                    'cached': sum(c.run_id != result.run_id for c in result.cells),
                    'flagged': sum(c.needs_review for c in result.cells),
                }
                return result.run_id
            except Exception:
                # Never pass provider exceptions to start_job's public error or log.
                raise RuntimeError('Analysis failed; check model settings and try again.') from None
            finally:
                conn.close()

        return self.start_job(request.task, project, work)

    def versions_link_job(self, body: dict[str, Any]) -> Job:
        """Link versions in the background: online registry lookups can take a while."""
        project = str(body.get("project") or "")
        conn = self.db()
        try:
            pid = get_project_id(conn, project) if project else None
        finally:
            conn.close()

        def work(job: Job) -> None:
            conn = self.db()
            try:
                job.messages.append("looking up version links")
                linked, suggested = link_project_versions(conn, self.make_http(),
                                                          contact_email(self.paths, self.config), pid)
                job.report = {"linked": linked, "suggested": suggested}
                job.messages.append(f"done: {linked} links, {suggested} suggestions")
            finally:
                conn.close()

        return self.start_job("versions-link", project, work)

    def console_job(self, run_id: str, project: str, approved_cloud: bool = False) -> Job:
        def work(job: Job) -> str:
            conn = self.db()
            try:
                saved = planner.get_run(conn, run_id)
                result = planner.execute_run(
                    conn, self.make_gateway(conn), self.paths, saved.project_id, run_id, approved=True,
                    approved_cloud=approved_cloud,
                    sources=self.sources() if any(step.action == "refresh" for step in saved.plan.steps) else None,
                    progress=job.messages.append,
                )
                job.report = {"status": result.status, "current_step": result.current_step,
                              "results": result.results}
                return run_id
            finally:
                conn.close()

        job = self.start_job("console", project, work)
        job.run_id = run_id
        return job

    def asset_job(self, asset_id: str, request: api.AssetAnalysisRequest) -> Job:
        conn = self.db()
        try:
            row = conn.execute(
                """SELECT p.id,p.title FROM assets a JOIN parse_runs r ON r.id=a.parse_run_id
                   JOIN artifacts f ON f.id=r.artifact_id JOIN papers p ON p.id=f.paper_id WHERE a.id=?""",
                (asset_id,),
            ).fetchone()
            if row is None:
                raise LookupError("asset not found")
            project = str(row["title"] or row["id"])
        finally:
            conn.close()

        def work(job: Job) -> str:
            conn = self.db()
            try:
                gateway = self.make_gateway(conn)
                preview = figures.plan_asset(conn, gateway, asset_id, request.mode, request.profile)
                if preview.blocked_reason or (preview.requires_approval and not request.approved):
                    raise ValueError("analysis plan needs approval or is blocked")
                result = figures.analyze_asset(conn, gateway, self.paths, asset_id, request.mode,
                                               request.profile, request.approved)
                job.report = result.model_dump()
                return result.run_id
            except Exception:
                raise RuntimeError("Asset analysis failed; check model settings and the selected crop.") from None
            finally:
                conn.close()

        return self.start_job("asset_analyze", project, work)

    def gap_design_job(self, request: api.GapDesignRequest) -> Job:
        conn = self.db()
        try:
            row = conn.execute(
                """SELECT p.name FROM gap_candidates g JOIN projects p ON p.id=g.project_id WHERE g.id=?""",
                (request.gap_id,),
            ).fetchone()
            if row is None:
                raise LookupError("gap candidate not found")
            project = str(row["name"])
        finally:
            conn.close()

        def work(job: Job) -> str:
            conn = self.db()
            try:
                gateway = self.make_gateway(conn)
                preview = gaps.plan_gap(conn, gateway, request.gap_id, request.profile)[0]
                if preview.blocked_reason or (preview.requires_approval and not request.approved):
                    raise ValueError("proposal plan needs approval or is blocked")
                card = gaps.design_gap(conn, gateway, request.gap_id, request.profile, request.approved)
                job.report = card.model_dump()
                return card.run_id
            except Exception:
                raise RuntimeError("Proposal design failed; check model settings and evidence.") from None
            finally:
                conn.close()

        return self.start_job("gap_design", project, work)

    def refresh_job(self, request: api.RefreshRequest) -> Job:
        conn = self.db()
        try:
            pid = get_project_id(conn, request.project)
            repair_abandoned_runs(conn)
            if conn.execute("SELECT 1 FROM watch_runs WHERE project_id=? AND status='running'", (pid,)).fetchone():
                raise ValueError("a refresh is already running in this project")
        finally:
            conn.close()

        def work(job: Job) -> str:
            conn = self.db()
            try:
                sources = self.sources()
                sources.http.refresh = True
                result = living.refresh_project(conn, self.make_gateway(conn), sources, pid,
                    screen=request.screen, extract=request.extract, claims=request.claims,
                    progress=job.messages.append, approved_cloud=request.approved)
                job.report = result.model_dump()
                return result.id
            except Exception:
                raise RuntimeError("Refresh failed; check criteria, model settings and discovery sources.") from None
            finally:
                conn.close()

        return self.start_job("refresh", request.project, work)

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

    def links_job(self, body: dict[str, Any]) -> Job:
        """Add papers from links (article pages, doi.org/arXiv/OpenAlex links): each paper joins the project and
        its open-access PDF is downloaded and read; direct PDF links are read like the user's own PDFs."""
        links = [str(u).strip() for u in body.get("links") or [] if str(u).strip()]
        if not links:
            raise ValueError("paste at least one link")
        if len(links) > 50:
            raise ValueError("at most 50 links at a time")
        conn = self.db()
        project = str(body.get("project") or "default")
        pid = self.ensure_project(conn, project, body.get("export_dir"))
        conn.close()

        def work(job: Job) -> str | None:
            c = self.db()
            pdfs: list[str] = []
            ids: list[str] = []
            try:
                report, pdfs = run_links(c, self.sources(), self.paths, pid, links, progress=job.messages.append)
                job.messages += [f"warning: {e}" for e in report.errors]
                ids = api.run_paper_ids(c, report.run_id, limit=len(links))
                if ids:
                    found = fetch_open_access(c, self.paths, self.config, self.make_http(), pid, "all", ids,
                                              self.log, progress=job.messages.append,
                                              mailto=contact_email(self.paths, self.config))
                    job.messages.append(f"{found.downloaded} of {found.candidates} missing PDFs downloaded, "
                                        f"{found.parsed} with full text" + (f"; {found.no_source} have no "
                                        "open-access copy" if found.no_source else ""))
                run_id = report.run_id
                if pdfs:
                    added, _results, _parsed = add_pdfs(c, self.paths, self.config, pid, [Path(f) for f in pdfs],
                                                       "PDF links", self.log, progress=job.messages.append)
                    run_id = run_id if ids else added.run_id
            finally:
                c.close()
                for f in pdfs:  # the library keeps its own copy
                    Path(f).unlink(missing_ok=True)
            job.messages.append(f"done: {len(ids)} papers from links"
                                + (f", {len(pdfs)} PDF link(s) read" if pdfs else ""))
            return run_id

        return self.start_job("links", project, work)

    def add_job(self, body: dict[str, Any]) -> Job:
        """Add PDFs from a folder on this computer and/or files uploaded from the page. With `export`
        ({"only": ..., "paper_ids": ...}, as for /api/export) the project's notes are exported afterwards.
        The export keeps the usual selection, not just the added papers: an export makes the folder match
        its selection, so exporting the added papers alone would remove every other note."""
        export = body.get("export")
        if export is not None and not isinstance(export, dict):
            raise ValueError("export must be an object like {\"only\": \"all\"}")
        if export is not None and str(export.get("only") or "all") not in MARK_SCOPES:
            raise ValueError(f"export.only must be one of {sorted(MARK_SCOPES)}")
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
            summary = f"{len(results)} files, {new} new, {known} already in the library; {full} with full text"
            if export is None:
                job.messages.append(f"done: {summary}")
                return report.run_id
            job.messages.append(summary)
            paper_ids = export.get("paper_ids")
            if paper_ids is not None:  # the chosen papers, and the ones the PDFs went to
                paper_ids = list(dict.fromkeys([*paper_ids, *(r.paper_id for r in results if r.paper_id)]))
            c = self.db()
            try:
                out, count = export_markdown(c, self.paths, project, pid, only=str(export.get("only") or "all"),
                                             config=self.config,
                                             paper_ids=paper_ids, obsidian=export.get("obsidian") is True,
                                             all_versions=export.get("all_versions") is True)
            except OSError as exc:  # the PDFs are in; say why the notes are not, and keep the job done
                job.report["export_error"] = str(exc)
                job.messages += [f"warning: export failed: {exc}", f"done: {summary}"]
                return report.run_id
            finally:
                c.close()
            job.report["exported"] = {"papers": count, "path": str(out)}
            job.messages.append(f"done: {summary}; exported {count} papers to {out}")
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
                profile = str(body.get("profile") or "") or None
                gateway = (self.make_gateway(c) if model is None and
                           (profile is not None or "screen" in self.config.models.tasks) else None)
                screen_run(c, self.make_llm(model) if gateway is None else None, run_id, top,
                           progress=job.messages.append, log=self.log, gateway=gateway,
                           profile=profile if gateway else None, approved=body.get("approved") is True)
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
        if level == "high" and self.config.vision.engine == "nemotron" and not nemotron.available(self.paths):
            raise ValueError("better math (high): Nemotron Parse is not set up; set it up in Settings")
        nemotron_serves = level == "high" and self._nemotron_serves()
        engine: Vision | FormulaEngine | None = (self.make_vision() if level == "high" and self.make_vision
                                                 and not nemotron_serves else None)
        if isinstance(engine, OllamaVision):
            try:
                engine.check()  # fail now, on the page, if Ollama or the model is missing
            except (RuntimeError, ValueError) as exc:
                raise ValueError(f"better math (high): {exc}") from exc

        if level == "high" and self.make_vision is None and not nemotron_serves:
            probe_conn = self.db()
            try:
                GatewayVision(self.make_gateway(probe_conn), body.get("profile"),
                              body.get("approved") is True).check()
            except (RuntimeError, ValueError) as exc:
                raise ValueError(f"better math (high): {exc}") from exc
            finally:
                probe_conn.close()

        def work(job: Job) -> str | None:
            c = self.db()
            job.report = {"library": str(self.paths.pdfs),
                          "export_dir": str(default_export_dir(c, self.paths, project, pid).expanduser().resolve())}
            try:
                if fetch:
                    self._fetch(c, job, pid, only, paper_ids)
                if level != "off":
                    def model() -> Vision:
                        made = self.make_vision() if self.make_vision else GatewayVision(
                            self.make_gateway(c), body.get("profile"), body.get("approved") is True)
                        if isinstance(made, GatewayVision):
                            made.check()
                        return made

                    tool = (engine if engine is not None else page_engine(self.paths, self.config, model)
                            if level == "high" else self.formulas())
                    if isinstance(tool, RoutedVision):
                        job.messages.append("Nemotron Parse rewrites English pages; " + (
                            f"{tool.model.name} the Persian ones" if tool.model else
                            "Persian pages keep their text (no vision model)"))
                    try:
                        vis = enhance_project(c, self.paths, self.config, pid, tool, only, paper_ids, self.log,
                                              progress=job.messages.append)
                    finally:
                        nemotron.release()
                    job.report["vision"] = {**dataclasses.asdict(vis), "level": level}
                    job.messages.append(f"better math ({level}): {vis.pages_improved} pages rewritten in "
                                        f"{vis.papers} papers" + (f", {vis.pages_rejected} kept as extracted"
                                                                 if vis.pages_rejected else ""))
                if export:
                    out, count = export_markdown(c, self.paths, project, pid, only=only, paper_ids=paper_ids,
                                                 config=self.config, all_versions=body.get("all_versions") is True,
                                                 obsidian=body.get("obsidian") is True)
                    job.messages.append(f"done: exported {count} papers to {out}")
            finally:
                c.close()
            return body.get("run_id")

        return self.start_job("export" if export else "fetch", project, work)

    def _uses_model_settings(self) -> bool:
        return bool(self.config.models.vision_profile or self.config.models.setup_tier or
                    "page_rewrite" in self.config.models.tasks or
                    self.config.vision.model is None)

    def math_setup_status(self) -> dict[str, Any]:
        """The vision model's readiness, with Nemotron Parse's: when Nemotron serves Math: high, a missing vision
        model only costs the Persian pages, so the status counts as ready and says so."""
        status = self._model_setup_status()
        nemo = nemotron_install_status(self.paths)
        nemo.pop("root", None)
        status["nemotron"] = {**nemo, "serves": self._nemotron_serves()}
        if status["nemotron"]["serves"] and not status.get("ready"):
            status.update(ready=True, disabled=False, partial=True,
                          message="Nemotron Parse rewrites English pages; Persian pages need the vision model "
                                  "and keep their extracted text until it is set up")
        return status

    def _model_setup_status(self) -> dict[str, Any]:
        if not self._uses_model_settings():
            return self.vision_setup.status().as_dict()
        conn = self.db()
        try:
            gateway = self.make_gateway(conn)
            _, cfg = gateway.resolve("page_rewrite", vision=True)
            message = ""
            if not cfg.vision:
                message = "Image features are off for this setup. Choose a vision model in Settings."
            elif cfg.remote and (not self.config.privacy.allow_cloud or self.config.models.policy == "local_only"):
                message = "Cloud page images are disabled. Review cloud permission in Settings first."
            return {"ready": not message, "disabled": bool(message), "message": message,
                    "model": cfg.model, "remote": cfg.remote, "notice": gateway._model_notice(cfg)}
        finally:
            conn.close()

    def setup_job(self, body: dict[str, Any]) -> Job:
        """Get better math (high) ready: install and start Ollama, pull the vision model (the user agreed)."""
        nemo = nemotron_install_status(self.paths)
        if self._uses_model_settings() and not nemo["offerable"]:
            raise ValueError("Use Settings for this setup; no tools are installed here.")
        if any(j.kind in ("setup", "nemotron-install") and j.status == "running" for j in self.jobs.values()):
            raise ValueError("the vision model is already being set up")

        def work(job: Job) -> str | None:
            # On an NVIDIA GPU, Nemotron Parse first: it is the smaller download and serves English pages.
            if nemo["offerable"]:
                install_nemotron(self.paths, self._job_progress(job))
            if not self._uses_model_settings():
                try:
                    self.vision_setup.run(job.messages.append)
                except RuntimeError as exc:
                    if not nemotron.available(self.paths):
                        raise
                    job.messages.append(f"The image model is not set up ({exc}); Nemotron Parse rewrites English "
                                        "pages, Persian pages keep their extracted text.")
            return None

        return self.start_job("setup", str(body.get("project") or "default"), work)

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


def _console_run_json(run: planner.ConsoleRun) -> dict[str, Any]:
    return {**run.model_dump(), "plan": run.plan.model_dump(), "results": run.results}


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
            if length > MAX_JSON_BODY:  # refused unread, before any memory is spent on it
                self.close_connection = True
                raise BodyTooLarge(f"request too large: at most {MAX_JSON_BODY // (1024 * 1024)} MB")
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _screen_info(self) -> dict[str, Any]:
            return {"model": app.config.screening.model, "default_top": app.config.screening.default_top}

        def _local_host(self) -> bool:
            """Refuse DNS rebinding: a page on evil.example rebound to 127.0.0.1 sends Host: evil.example."""
            raw = self.headers.get("Host")
            if raw is None:  # browsers always send Host; other local clients may not
                return True
            bound = str(getattr(getattr(self, "server", None), "server_address", ("",))[0])
            if bound in {"0.0.0.0", "::"}:  # the user chose to serve the LAN (--host); any address is expected
                return True
            return urlsplit("//" + raw).hostname in {"127.0.0.1", "localhost", "::1", bound}

        def _same_origin(self) -> bool:
            """Reject cross-site requests that would otherwise reach a localhost API."""
            origin = self.headers.get("Origin")
            return self._local_host() and (origin is None or urlsplit(origin).netloc == self.headers.get("Host"))

        # ---- routing ----

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if not self._local_host():
                return self._error(HTTPStatus.FORBIDDEN, "unknown host")
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                self._get(url.path, query)
            except LookupError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
            except (ValueError, ProjectNotFound) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))

        def _get(self, path: str, query: dict[str, str]) -> None:
            if path == '/api/update':
                return self._json(app.updates.get())
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            if self._is_lens(path):
                return self._lens(path, query, None)
            conn = app.db()
            try:
                if path == "/api/mineru":
                    return self._json(mineru_status(app.paths, app.config.parsing))
                if path == "/api/tools":
                    return self._json(app.optional_tools_status())
                if path.startswith("/api/review/"):
                    result = review_routes.get(app, conn, path, query)
                    return self._json(*result) if isinstance(result, tuple) else self._json(result)
                if path.startswith("/api/write/"):
                    result = writing_routes.get(app, conn, path, query)
                    if isinstance(result, tuple):
                        disposition = result[2] if len(result) > 2 else "inline"
                        return self._send(200, result[0], result[1], {"Content-Disposition": disposition})
                    return self._json(result)
                if path == "/api/keys":
                    return self._json(app.keys.status())
                if path == "/api/models/catalogue":
                    return self._json(app.models.catalogue.catalogue(vision=query.get("vision") == "1",
                        max_input_price=float(query["max_input_price"]) if query.get("max_input_price") else None,
                        q=query.get("q", "")))
                if path == "/api/models/local-search":
                    return self._json(app.models.hf.search(query.get("q", ""), show_all=query.get("show_all") == "1"))
                if path == "/api/models/local-files":
                    tree = app.models.hf.files(query.get("repo", ""))
                    for item in tree["models"]:
                        item["fit"] = fit_check(app.models._detect(), item["size"])
                    return self._json(tree)
                if path == "/api/models":
                    return self._json(app.models.info())
                if path == "/api/model-profiles":
                    return self._json(api.model_profiles(app.config))
                if path == "/api/running":  # read-only, for closing the desktop window
                    return self._json({"running": sum(j.status == "running" for j in app.jobs.values())})
                if path == "/api/info":
                    return self._json({"exports_root": str(app.paths.exports_root), "desktop": app.desktop,
                                       "math": {"medium": formulas_available(), "high": app.config.vision.model,
                                                "high_cloud": math_high_cloud(app.config)}})
                if path == "/api/math-setup":
                    return self._json(app.math_setup_status())
                if path == "/api/projects":
                    return self._json(api.list_projects(conn, app.paths))
                if path == "/api/runs":
                    pid = get_project_id(conn, query["project"]) if query.get("project") else None
                    return self._json(api.list_runs(conn, pid, int(query.get("limit", 50))))
                if path == "/api/library":
                    return self._json(api.library(conn, include_versions=query.get("all_versions") == "1"))
                if path == "/api/library/removal-preview":
                    raw_ids = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('paper_ids')
                    ids = [p for value in raw_ids for p in value.split(',') if p] if raw_ids is not None else None
                    return self._json(api.library_removal_preview(conn, app.paths, ids))
                if path == "/api/settings":
                    return self._json({"mailto": contact_email(app.paths, app.config),
                                       "update_check": load_settings(app.paths).get('update_check', True),
                                       "update_check_enabled": automatic_enabled(app.paths),
                                       "exports_root": str(app.paths.exports_root)})
                if path == "/api/missing-pdfs":
                    pid = get_project_id(conn, query["project"])
                    return self._json(api.missing_pdfs(conn, pid, query.get("only", "all")))
                if path == "/api/criteria":
                    return self._json({**api.get_criteria(conn, query["project"]), **self._screen_info()})
                if m := re.fullmatch(r"/api/runs/([\w-]+)", path):
                    return self._json(api.run_results(conn, m.group(1), query.get("all_versions") == "1"))
                if m := re.fullmatch(r"/api/jobs/(\w+)", path):
                    job = app.jobs.get(m.group(1))
                    if job is None:
                        raise LookupError("job not found")
                    return self._json(job.__dict__)
                if m := re.fullmatch(r"/api/papers/([\w-]+)", path):
                    project_id = get_project_id(conn, query["project"]) if query.get("project") else None
                    return self._json(api.paper_detail(conn, app.paths, m.group(1), project_id, app.config))
                if m := re.fullmatch(r"/api/papers/([\w-]+)/pdf", path):
                    pdf, name = api.paper_pdf(conn, app.paths, m.group(1))
                    return self._send(200, pdf.read_bytes(), "application/pdf",
                                      {"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
                if m := re.fullmatch(r"/api/assets/([\w-]+)/crop", path):
                    target = api.asset_crop(conn, app.paths, m.group(1))
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self._send(200, target.read_bytes(), ctype)
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

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._same_origin():
                return self._error(403, "cross-origin request refused")
            if urlsplit(self.path).path != "/api/keys":
                return self._error(404, "not found")
            try:
                app.keys.remove(self._body().get("name"))
                self._json({"ok": True})
            except ValueError:
                self._error(400, "Could not remove key; use a known key name and check the system keychain")

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            if not self._same_origin():
                return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            url = urlsplit(self.path)
            try:
                if url.path == "/api/upload":
                    return self._upload(parse_qs(url.query).get("name", ["paper.pdf"])[0])
                if url.path == "/api/heartbeat":
                    app.last_seen = time.monotonic()
                    return self._json({"ok": True})
                if url.path == "/api/import":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    return self._import(q.get("name", "refs.csv"), q.get("project") or "default", q.get("export_dir"))
                if url.path == "/api/write/import":
                    return self._write_import({k: v[0] for k, v in parse_qs(url.query).items()})
                if url.path == "/api/write/upload":
                    return self._write_upload({k: v[0] for k, v in parse_qs(url.query).items()})
                try:
                    body = self._body()
                except BodyTooLarge as exc:
                    return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
                if url.path.startswith("/api/review/"):
                    conn = app.db()
                    try:
                        result = review_routes.post(app, conn, url.path, body)
                        return self._json(*result) if isinstance(result, tuple) else self._json(result)
                    finally:
                        conn.close()
                if url.path.startswith("/api/write/"):
                    return self._write_post(url.path, body)
                if m := re.fullmatch(r"/api/jobs/(\w+)/stop", url.path):
                    return self._json(app.stop_job(m[1]).__dict__)
                if url.path == "/api/tools/tectonic/install":
                    return self._json(writing_routes.install_tectonic_job(app, body).__dict__)
                if url.path == '/api/update/check':
                    return self._json(app.updates.force())
                if url.path == "/api/mineru/install":
                    return self._json(app.mineru_install_job(body).__dict__)
                if url.path == "/api/tools/pix2text/install":
                    return self._json(app.pix2text_install_job(body).__dict__)
                if url.path == "/api/tools/nemotron/install":
                    return self._json(app.nemotron_install_job(body).__dict__)
                if url.path == "/api/tools/desktop/check":
                    return self._json(app.desktop_check_job().__dict__)
                if url.path == "/api/tools/engines":
                    return self._json(app.save_engines(body))
                if m := re.fullmatch(r"/api/papers/([\w-]+)/parse-mineru", url.path):
                    return self._json(app.mineru_parse_job(m[1]).__dict__)
                if url.path == "/api/library/remove":
                    conn = app.db()
                    try:
                        return self._json(api.library_remove(conn, app.paths, body.get('paper_ids'),
                                                             all_unused=body.get('all_unused', False)))
                    finally:
                        conn.close()
                if url.path == "/api/versions/set-primary":
                    conn = app.db()
                    try:
                        set_primary(conn, str(body.get("paper_id") or ""))
                        return self._json({"ok": True})
                    finally:
                        conn.close()
                if url.path in ("/api/versions/confirm", "/api/versions/reject"):
                    conn = app.db()
                    try:
                        a, b = str(body.get("a") or ""), str(body.get("b") or "")
                        if url.path.endswith("/confirm"):
                            confirm_version_link(conn, a, b)
                        elif not reject_version_link(conn, a, b):
                            raise ValueError("no pending suggestion links these papers")
                        return self._json({"ok": True})
                    finally:
                        conn.close()
                if url.path == "/api/versions/set":
                    conn = app.db()
                    try:
                        api.set_paper_version(conn, str(body.get("paper_id") or ""), body.get("version"))
                        return self._json({"ok": True})
                    finally:
                        conn.close()
                if url.path == "/api/versions/unlink":
                    conn = app.db()
                    try:
                        if not unlink_version(conn, str(body.get("paper_id") or "")):
                            raise ValueError("this paper is not in a confirmed version group")
                        return self._json({"ok": True})
                    finally:
                        conn.close()
                if url.path == "/api/versions/link":
                    return self._json(app.versions_link_job(body).__dict__)
                if url.path == "/api/keys":
                    app.keys.set(body.get("name"), body.get("value"))
                    return self._json({"ok": True})
                if url.path == "/api/models/download-preview":
                    return self._json(app.models.prepare_download(body))
                if url.path == "/api/models/download":
                    return self._json(app.download_job(body).__dict__)
                if url.path == "/api/models/download-control":
                    return self._json(app.download_control(body).__dict__)
                if url.path == "/api/models/pull-ollama":
                    return self._json(app.ollama_pull_job(body).__dict__)
                if url.path == "/api/export-preview":
                    conn = app.db()
                    try:
                        chosen = page_engine(app.paths, app.config,
                                             lambda: GatewayVision(app.make_gateway(conn), body.get("profile")))
                        engine = chosen.model if isinstance(chosen, RoutedVision) else chosen
                        pages = plan_project(
                            conn, app.paths, get_project_id(conn, str(body["project"])), engine,
                            str(body.get("only", "all")), body.get("paper_ids"),
                            routed=chosen if isinstance(chosen, RoutedVision) else None,
                        ) if isinstance(engine, GatewayVision) else []
                        return self._json({"page_images": len(pages),
                            "image_description": f"{len(pages)} page images will be sent",
                            "blocked_reasons": list(dict.fromkeys(p.blocked_reason for p in pages if p.blocked_reason)),
                            "max_cost_usd": sum(p.max_cost_usd or 0 for p in pages)})
                    finally:
                        conn.close()
                if url.path == "/api/models/preview":
                    return self._json(app.models.preview(body))
                if url.path == "/api/models/apply":
                    app.models.apply(str(body.get("token", "")), body.get("confirmed") is True)
                    app.config = app.models.config
                    app.keys.names.update(KeyStore(app.config).names)
                    return self._json({"ok": True})
                if url.path == "/api/models/test":
                    return self._json(app.model_test_job(body).__dict__)
                if self._is_lens(url.path):
                    return self._lens(url.path, {}, body)
                if url.path == "/api/similar":
                    return self._json(app.similar_job(body).__dict__)
                if url.path == "/api/search":
                    return self._json(app.search_job(body).__dict__)
                if url.path == "/api/add":
                    return self._json(app.add_job(body).__dict__)
                if url.path == "/api/links":
                    return self._json(app.links_job(body).__dict__)
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
                    open_with_system(folder)
                    return self._json({"path": str(folder)})
                if url.path == "/api/open-pdf":  # the desktop window shows PDFs in the system's PDF app
                    conn = app.db()
                    try:
                        if body.get("paper"):
                            pdf = api.paper_pdf(conn, app.paths, str(body["paper"]))[0]
                        elif body.get("doc"):
                            doc = wd.get_document(conn, app.paths, str(body["doc"]))
                            pdf = wd.resolve_inside(doc.folder, doc.output(".pdf"), allow_hidden=True)
                            if not pdf.is_file():
                                raise LookupError("no PDF yet; compile the document first")
                        else:
                            raise ValueError("give a paper or a document")
                    finally:
                        conn.close()
                    open_with_system(pdf)
                    return self._json({"opened": str(pdf)})
                if url.path == "/api/quit":  # the Quit button: LitForge may run without a terminal to close
                    if app.stop is None:
                        raise ValueError("this server cannot be stopped from the page")
                    running = sum(j.status == "running" for j in app.jobs.values())
                    if running and not body.get("force"):
                        return self._json({"stopped": False, "running": running})
                    threading.Thread(target=app.stop, daemon=True).start()
                    return self._json({"stopped": True})
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
                    if 'update_check' in body and not isinstance(body['update_check'], bool):
                        raise ValueError('update_check must be a boolean')
                    if 'mailto' in body:
                        save_mailto(app.paths, body['mailto'])
                    if 'update_check' in body:
                        save_update_check(app.paths, body['update_check'])
                    return self._json({"mailto": contact_email(app.paths, app.config),
                                       "update_check": load_settings(app.paths).get('update_check', True),
                                       "update_check_enabled": automatic_enabled(app.paths),
                                       "exports_root": str(app.paths.exports_root)})
                if url.path == "/api/math-setup":
                    return self._json(app.setup_job(body).__dict__)
                if url.path == "/api/fetch":
                    return self._json(app.export_job(body, export=False).__dict__)
                if url.path == "/api/export" and (body.get("fetch") or body.get("vision") or body.get("math")):
                    return self._json(app.export_job(body).__dict__)
                if url.path == "/api/export":
                    conn = app.db()
                    try:
                        pid = app.ensure_project(conn, body["project"], body.get("export_dir"))
                        out, count = export_markdown(conn, app.paths, body["project"], pid, config=app.config,
                                                     only=body.get("only", "all"), paper_ids=body.get("paper_ids"),
                                                     all_versions=bool(body.get("all_versions", False)),
                                                     obsidian=body.get("obsidian") is True)
                    finally:
                        conn.close()
                    return self._json({"path": str(out), "papers": count})
                self._error(HTTPStatus.NOT_FOUND, "not found")
            except (ValueError, KeyError, ProjectNotFound, json.JSONDecodeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except LookupError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))

        def _is_lens(self, path: str) -> bool:
            return (path in ("/api/ask", "/api/audit", "/api/preview", "/api/review", "/api/matrices",
                             "/api/queue", "/api/screen-fulltext", "/api/snapshots",
                             "/api/refresh", "/api/repro/matrix", "/api/gaps", "/api/gaps/detect",
                             "/api/gaps/design", "/api/gaps/review", "/api/console/plan",
                             "/api/console/execute", "/api/console/resume", "/api/console/cancel",
                             "/api/console/runs")
                    or path.startswith(("/api/answers/", "/api/matrices/", "/api/claims"))
                    or path.startswith("/api/console/runs/")
                    or bool(re.fullmatch(r"/api/assets/[\w-]+/analyze", path))
                    or bool(re.fullmatch(r"/api/gaps/cards/[\w-]+/export", path))
                    or bool(re.fullmatch(r"/api/papers/[\w-]+/repro", path)))

        def _lens(self, path: str, query: dict[str, str], body: dict[str, Any] | None) -> None:
            conn = app.db()
            try:
                if body is None:
                    if match := re.fullmatch(r"/api/console/runs/([\w-]+)/exports/([A-Za-z0-9_.-]+)", path):
                        run = planner.get_run(conn, match[1])
                        export_paths = [item["output"].get("path") for item in run.results
                                        if isinstance(item.get("output"), dict)]
                        if not any(isinstance(value, str) and Path(value).name == match[2]
                                   for value in export_paths):
                            raise LookupError("export not found for this console run")
                        project_row = (conn.execute("SELECT name FROM projects WHERE id=?",
                                                     (run.project_id,)).fetchone()
                                       if run.project_id else None)
                        if project_row is None or run.project_id is None:
                            raise LookupError("project export not found")
                        base = default_export_dir(conn, app.paths, project_row["name"], run.project_id).resolve()
                        target = (base / match[2]).resolve()
                        if base not in target.parents or not target.is_file():
                            raise LookupError("export not found")
                        return self._send(200, target.read_bytes(),
                                          mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                                          {"Content-Disposition": f'attachment; filename="{target.name}"'})
                    if path == "/api/console/runs":
                        project_id = get_project_id(conn, query["project"]) if query.get("project") else None
                        return self._json([_console_run_json(item) for item in
                                           planner.list_runs(conn, project_id, int(query.get("limit", "50")))])
                    if match := re.fullmatch(r"/api/console/runs/([\w-]+)", path):
                        return self._json(_console_run_json(planner.get_run(conn, match[1])))
                    if path == "/api/gaps":
                        gap_pid = get_project_id(conn, query.get("project", ""))
                        return self._json(api.gap_panel(conn, gap_pid))
                    if match := re.fullmatch(r"/api/gaps/cards/([\w-]+)/export", path):
                        return self._json({"markdown": gaps.export_gap_card(conn, match[1])})
                    if path == "/api/snapshots":
                        return self._json(api.living_panel(conn, get_project_id(conn, query.get("project", ""))))
                    if match := re.fullmatch(r"/api/papers/([\w-]+)/repro", path):
                        repro_pid = get_project_id(conn, query["project"]) if query.get("project") else None
                        return self._json(repro.repro_card(conn, match[1], repro_pid,
                                                          refresh=False).model_dump(exclude={"brief"}))
                    if path.startswith("/api/claims") or path == "/api/queue":
                        phase2_pid = get_project_id(conn, query.get("project", ""))
                        if path == "/api/queue":
                            items = active.review_queue(
                                conn, phase2_pid, int(query.get("limit", "20")),
                                all_versions=query.get("all_versions") == "1")
                            return self._json([i.model_dump() for i in items])
                        if path == "/api/claims/landscape":
                            return self._json([i.model_dump() for i in claim_graph.claim_landscape(conn, phase2_pid)])
                        if path == "/api/claims":
                            return self._json(api.lens_claims(
                                conn, phase2_pid, query.get("cluster"), query.get("all_versions") == "1"))
                        if match := re.fullmatch(r"/api/claims/clusters/([\w-]+)", path):
                            return self._json(api.lens_claims(
                                conn, phase2_pid, match[1], query.get("all_versions") == "1"))
                    if path == '/api/review':
                        review_pid = get_project_id(conn, query['project']) if query.get('project') else None
                        return self._json(api.lens_reviews(conn, review_pid))
                    if path == '/api/matrices':
                        matrix_pid = get_project_id(conn, query['project']) if query.get('project') else None
                        return self._json([m.model_dump(exclude={'cells'})
                                           for m in matrices.list_matrices(conn, matrix_pid)])
                    if match := re.fullmatch(r'/api/answers/([\w-]+)', path):
                        return self._json(api.lens_answer(conn, match[1]))
                    if match := re.fullmatch(r'/api/matrices/([\w-]+)(/export)?', path):
                        if match[2]:
                            fmt = query.get('format', 'md')
                            types = {'md': 'text/markdown', 'csv': 'text/csv', 'json': 'application/json'}
                            if fmt not in types:
                                raise ValueError('invalid export format')
                            content = matrices.export_matrix(conn, match[1], fmt)
                            headers = {'Content-Disposition': f'attachment; filename="matrix-{match[1]}.{fmt}"'}
                            return self._send(200, content.encode(), types[fmt] + '; charset=utf-8', headers)
                        return self._json(api.lens_matrix(conn, match[1], query.get("all_versions") == "1"))
                else:
                    if path == "/api/console/plan":
                        request_text = str(body.get("request") or "")
                        project_name = str(body["project"]) if body.get("project") else None
                        project_id = get_project_id(conn, project_name) if project_name else None
                        gateway = app.make_gateway(conn)
                        cloud_approved = body.get("approved_cloud") is True
                        profile = str(body.get("profile") or "") or None
                        planner_call = planner.preview_planner_call(gateway, request_text, profile)
                        if planner_call.blocked_reason:
                            return self._json({"error": "Planner route is blocked.",
                                               "preview": planner_call.model_dump()}, 400)
                        if planner_call.requires_approval and not cloud_approved:
                            return self._json({"error": "Separate approval is required for the cloud planner route.",
                                               "preview": planner_call.model_dump()}, 403)
                        plan_value = (planner.plan(conn, gateway, request_text, project_id, cloud_approved, profile)
                                      if profile else
                                      planner.plan(conn, gateway, request_text, project_id, cloud_approved))
                        project_row = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone() \
                            if project_id else None
                        context = ActionContext(conn, gateway, project_id,
                                                str(project_row["name"]) if project_row else None, app.paths)
                        estimate = planner.preview_plan(plan_value, context,
                                                        app.config.console.seconds_per_model_call)
                        return self._json({"plan": plan_value.model_dump(), "preview": estimate.model_dump()})
                    if path == "/api/console/execute":
                        if body.get("approved") is not True:
                            return self._error(HTTPStatus.FORBIDDEN, "Explicit execution approval is required.")
                        raw_plan = body.get("plan")
                        if not isinstance(raw_plan, dict) or not isinstance(raw_plan.get("request"), str):
                            raise ValueError("plan must include its request and steps")
                        plan_value = planner.validate_plan({"steps": raw_plan.get("steps")},
                                                           raw_plan["request"])
                        project_name = str(body["project"]) if body.get("project") else None
                        project_id = get_project_id(conn, project_name) if project_name else None
                        gateway = app.make_gateway(conn)
                        project_row = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone() \
                            if project_id else None
                        context = ActionContext(conn, gateway, project_id,
                                                str(project_row["name"]) if project_row else None, app.paths)
                        estimate = planner.preview_plan(plan_value, context, app.config.console.seconds_per_model_call)
                        if estimate.blocked_reasons:
                            return self._json({"error": "One or more model routes are blocked.",
                                               "preview": estimate.model_dump()}, 400)
                        if estimate.requires_approval and body.get("approved_cloud") is not True:
                            return self._json({"error": "Separate cloud-route approval is required.",
                                               "preview": estimate.model_dump()}, 403)
                        run_id = planner.create_run(conn, project_id, plan_value)
                        job = app.console_job(run_id, project_name or "library", body.get("approved_cloud") is True)
                        return self._json({"run_id": run_id, "job": job.__dict__})
                    if path == "/api/console/resume":
                        if body.get("approved") is not True:
                            return self._error(HTTPStatus.FORBIDDEN, "Explicit execution approval is required.")
                        run_id = str(body.get("run_id") or "")
                        saved = planner.get_run(conn, run_id)
                        project_row = (conn.execute("SELECT name FROM projects WHERE id=?",
                                                     (saved.project_id,)).fetchone()
                                       if saved.project_id else None)
                        project_name = str(project_row["name"]) if project_row else "library"
                        gateway = app.make_gateway(conn)
                        context = ActionContext(conn, gateway, saved.project_id, project_name, app.paths)
                        estimate = planner.preview_plan(saved.plan, context, app.config.console.seconds_per_model_call)
                        if estimate.requires_approval and body.get("approved_cloud") is not True:
                            return self._json({"error": "Separate cloud-route approval is required.",
                                               "preview": estimate.model_dump()}, 403)
                        return self._json({"run_id": run_id,
                                           "job": app.console_job(run_id, project_name,
                                                                  body.get("approved_cloud") is True).__dict__})
                    if path == "/api/console/cancel":
                        run = planner.request_cancel(conn, str(body.get("run_id") or ""))
                        return self._json(_console_run_json(run))
                    if path == "/api/gaps/detect":
                        project_id = get_project_id(conn, str(body.get("project") or ""))
                        return self._json(api.gap_panel(conn, project_id, detect=True))
                    if match := re.fullmatch(r"/api/papers/([\w-]+)/repro", path):
                        repro_pid = get_project_id(conn, str(body["project"])) if body.get("project") else None
                        return self._json(repro.repro_card(conn, match[1], repro_pid).model_dump(exclude={"brief"}))
                    if match := re.fullmatch(r"/api/assets/([\w-]+)/analyze", path):
                        asset_request = api.AssetAnalysisRequest.model_validate(body)
                        gateway = app.make_gateway(conn)
                        preview = figures.plan_asset(conn, gateway, match[1], asset_request.mode,
                                                     asset_request.profile)
                        if preview.blocked_reason:
                            return self._json({"error": "Analysis route is blocked. Check model and budget settings.",
                                               "preview": preview.model_dump()}, 400)
                        if preview.requires_approval and not asset_request.approved:
                            return self._json({"error": "Approval required before sending the asset crop.",
                                               "preview": preview.model_dump()}, 403)
                        return self._json(app.asset_job(match[1], asset_request).__dict__)
                    if path == "/api/gaps/design":
                        gap_request = api.GapDesignRequest.model_validate(body)
                        preview = gaps.plan_gap(conn, app.make_gateway(conn), gap_request.gap_id,
                                                gap_request.profile)[0]
                        if preview.blocked_reason:
                            return self._json({"error": "Proposal route is blocked. Check model and budget settings.",
                                               "preview": preview.model_dump()}, 400)
                        if preview.requires_approval and not gap_request.approved:
                            return self._json({"error": "Approval required before sending project evidence.",
                                               "preview": preview.model_dump()}, 403)
                        return self._json(app.gap_design_job(gap_request).__dict__)
                    if path == "/api/gaps/review":
                        gap_review = api.GapReviewRequest.model_validate(body)
                        return self._json(gaps.review_gap(
                            conn, gap_review.card_id, gap_review.action, gap_review.reason,
                        ).model_dump())
                    if path == "/api/snapshots":
                        snapshot_request = api.SnapshotRequest.model_validate(body)
                        pid = get_project_id(conn, snapshot_request.project)
                        sid = snapshots.take_snapshot(conn, pid, snapshot_request.label)
                        return self._json(snapshots.get_snapshot(conn, sid).model_dump(exclude={"data"}))
                    if path == "/api/refresh":
                        return self._json(app.refresh_job(api.RefreshRequest.model_validate(body)).__dict__)
                    if path == "/api/repro/matrix":
                        snapshot_request = api.SnapshotRequest.model_validate(body)
                        pid = get_project_id(conn, snapshot_request.project)
                        return self._json(repro.ensure_repro_matrix(conn, pid).model_dump())
                    if path == "/api/claims/cluster":
                        phase2_pid = get_project_id(conn, str(body.get("project") or ""))
                        return self._json([c.model_dump() for c in claim_graph.cluster_claims(
                            conn, phase2_pid, float(body.get("threshold", .55)))])
                    if path == "/api/claims/review":
                        claim_review = api.ClaimReviewRequest.model_validate(body)
                        return self._json(claims.review_claim(conn, claim_review.id, claim_review.action,
                                                            claim_review.text, claim_review.reason).model_dump())
                    if path in ('/api/preview', '/api/ask', '/api/audit', '/api/claims/relate',
                                '/api/claims/extract', '/api/screen-fulltext') or path.endswith('/extract'):
                        values = dict(body)
                        if path != '/api/preview':
                            values['task'] = {'/api/ask': 'ask', '/api/audit': 'audit',
                                              '/api/claims/extract': 'claim_extract',
                                              '/api/claims/relate': 'claim_relation',
                                              '/api/screen-fulltext': 'screen_fulltext'}.get(path, 'matrix_extract')
                            if values['task'] == 'matrix_extract':
                                match = re.fullmatch(r'/api/matrices/([\w-]+)/extract', path)
                                if match is None:
                                    raise LookupError('not found')
                                values['matrix_id'] = match[1]
                        lens_request = api.LensRequest.model_validate(values)
                        plan = api.lens_plan(conn, app.make_gateway(conn), lens_request)
                        if path == '/api/preview':
                            return self._json(plan)
                        if plan['blocked_reasons']:
                            return self._json({'error': 'Analysis route is blocked. Check model and budget settings.',
                                               'preview': plan}, 400)
                        if plan['requires_approval'] and not lens_request.approved:
                            return self._json({'error': 'Approval required before sending evidence.',
                                               'preview': plan}, 403)
                        return self._json(app.lens_job(lens_request).__dict__)
                    if path == '/api/review':
                        review = api.ReviewRequest.model_validate(body)
                        rid = record_review(conn, review.target_kind, review.target_id, review.action,
                                            review.new_value, review.reason)
                        return self._json({'id': rid})
                    if path == '/api/matrices':
                        create = api.MatrixRequest.model_validate(body)
                        create_pid = get_project_id(conn, create.project) if create.project else None
                        matrix = matrices.create_matrix(conn, create_pid, create.name, create.question, create.columns)
                        return self._json(matrix.model_dump())
                    if match := re.fullmatch(r'/api/matrices/([\w-]+)/columns', path):
                        column = matrices.MatrixColumn.model_validate(body)
                        return self._json(matrices.add_column(conn, match[1], column).model_dump())
                self._error(404, 'not found')
            except (KeyError, LookupError):
                self._error(404, 'Evidence Lens item not found.')
            except planner.PlanValidationError as exc:
                self._json({"error": "Plan rejected; validation errors are shown.", "validation_errors": exc.errors},
                           HTTPStatus.BAD_REQUEST)
            except (ValidationError, ValueError, ProjectNotFound, sqlite3.IntegrityError):
                self._error(400, 'Invalid Evidence Lens request. Check fields and model settings.')
            except Exception:
                self._error(500, 'Evidence Lens failed. Check model settings and try again.')
            finally:
                conn.close()

        def _write_post(self, path: str, body: dict[str, Any]) -> None:
            conn = app.db()
            try:
                result = writing_routes.post(app, conn, path, body)
                if isinstance(result, tuple):
                    disposition = result[2] if len(result) > 2 else "inline"
                    return self._send(200, result[0], result[1], {"Content-Disposition": disposition})
                return self._json(result)
            except writing_routes.Conflict as exc:
                return self._json(exc.payload, HTTPStatus.CONFLICT)
            finally:
                conn.close()

        def _write_import(self, query: dict[str, str]) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= writing_routes.MAX_ZIP_UPLOAD:
                self.close_connection = True
                return self._error(HTTPStatus.BAD_REQUEST, "empty or too large ZIP upload (at most 100 MB)")
            data = self.rfile.read(length)
            if len(data) != length:
                return self._error(HTTPStatus.BAD_REQUEST, "incomplete ZIP upload")
            conn = app.db()
            try:
                return self._json(writing_routes.import_archive(app, conn, query, data))
            finally:
                conn.close()

        def _write_upload(self, query: dict[str, str]) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= writing_routes.MAX_FIGURE_UPLOAD:
                return self._error(HTTPStatus.BAD_REQUEST, "empty or too large upload (at most 25 MB)")
            data = self.rfile.read(length)
            conn = app.db()
            try:
                return self._json(writing_routes.upload(app, conn, query.get("doc", ""), query.get("name", ""), data))
            finally:
                conn.close()

        def _upload(self, name: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_UPLOAD_BYTES:
                return self._error(HTTPStatus.BAD_REQUEST, "empty or too large upload")
            data = self.rfile.read(length)
            if not data.startswith(b"%PDF-") and not is_document(Path(name)):
                return self._error(HTTPStatus.BAD_REQUEST, "not a PDF or a supported document")
            uploads = app.paths.root / "uploads"
            uploads.mkdir(parents=True, exist_ok=True)
            clean = re.sub(r"[^\w.\-]+", "_", Path(name).name)
            suffix = Path(clean).suffix[:10]  # a long name keeps its extension (review: "….docx" became "….do")
            safe = (clean[:100 - len(suffix)] + suffix) if len(clean) > 100 else clean or "paper.pdf"
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


def open_with_system(path: Path) -> None:
    """Show a file or folder in the system's own app for it (file manager, PDF viewer)."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],  # noqa: S603
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class ServerHandle:
    """A running server owned by its caller: serve() for the browser, the desktop window otherwise."""

    def __init__(self, app: App, httpd: ThreadingHTTPServer, thread: threading.Thread, url: str) -> None:
        self.app = app
        self._httpd = httpd
        self._thread = thread
        self._lock = threading.Lock()
        self._cleanup: threading.Thread | None = None
        self._closed = False
        self.url = url

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until the server stops serving (stop(), or Quit in the page); True once it has."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop serving, then cancel jobs and stop owned processes (App.shutdown) once and free the address.

        Safe to call again; raises TimeoutError while the cleanup is still running, so a caller never
        reports a clean exit that did not happen."""
        with self._lock:
            if self._closed:
                return
            if self._cleanup is None:
                self._httpd.shutdown()
                self._thread.join()
                self._cleanup = threading.Thread(target=self.app.shutdown, args=(timeout,), daemon=True,
                                                 name="litforge-shutdown")
                self._cleanup.start()
            self._cleanup.join(timeout)
            if self._cleanup.is_alive():
                raise TimeoutError(f"LitForge did not finish stopping within {timeout:g} s")
            self._httpd.server_close()
            self._closed = True


def start_server(paths: Paths, config: Config, *, host: str = "127.0.0.1", port: int = 0,
                 idle_shutdown: bool = False, desktop: bool = False) -> ServerHandle:
    """Serve the UI from a background thread and return at once; never opens a browser.

    `idle_shutdown` stops the server once no page has checked in for a while (browser mode only: a minimised
    desktop window may not check in). Quit in the page stops it in both modes."""
    app = App(paths, config)
    app.desktop = desktop
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    app.stop = httpd.shutdown
    if idle_shutdown:
        stop_when_idle(app, httpd)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="litforge-http")
    thread.start()
    return ServerHandle(app, httpd, thread, f"http://{host}:{httpd.server_address[1]}/")


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
    handle = start_server(paths, config, host=host, port=port, idle_shutdown=True)
    print(f"LitForge UI on {handle.url}  (stop it with Quit in the page or Ctrl+C)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(handle.url,)).start()
    try:
        while not handle.wait(0.5):  # a short wait keeps Ctrl+C responsive
            pass
    except KeyboardInterrupt:
        pass
    finally:
        handle.stop()


def stop_when_idle(app: App, httpd: ThreadingHTTPServer, idle_s: float = IDLE_STOP_S, check_s: float = 5.0) -> None:
    """Stop the server once no page has checked in for `idle_s` seconds, so closing the tab stops LitForge.

    A running job keeps it up. Background tabs run timers about once a minute, so the limit is longer than that.
    """
    def watch() -> None:
        while True:
            time.sleep(check_s)
            busy = any(j.status == "running" for j in app.jobs.values())
            if not busy and time.monotonic() - app.last_seen > idle_s:
                httpd.shutdown()
                return

    threading.Thread(target=watch, daemon=True, name="idle-watchdog").start()


def _is_litforge(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url + "api/info", timeout=2) as r:  # noqa: S310 (local address)
            return "exports_root" in json.loads(r.read())
    except (OSError, ValueError):
        return False
