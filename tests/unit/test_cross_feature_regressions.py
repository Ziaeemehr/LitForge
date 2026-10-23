"""Regressions from the first pre-merge integration review."""
from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import suppress
from http.client import HTTPResponse

import pytest
from discovery_fakes import FakeTransport, default_routes
from lens_fakes import evidence_id, seed
from typer.testing import CliRunner

from litforge import cli
from litforge.analysis import ask, figures, living, matrix, snapshots
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.matrix import MatrixColumn, create_matrix
from litforge.config import Config
from litforge.console import actions, planner
from litforge.console.actions import ActionContext
from litforge.discovery.http import HttpClient
from litforge.discovery.run import Sources
from litforge.indexing.fts import search
from litforge.parsing.mineru_parser import _run_command
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.llm import FakeLlm
from litforge.screening.run import preview_run
from litforge.storage import recovery
from litforge.storage.db import now
from litforge.ui import server


def cloud_config(task: str) -> Config:
    return Config.model_validate({"privacy": {"allow_cloud": True}, "models": {
        "policy": "ask_before_cloud", "tasks": {task: {"profile": "remote"}}, "profiles": {
            "remote": {"provider": "openai_compatible", "model": "cloud-test", "remote": True,
                       "base_url": "https://provider.invalid/v1", "api_key_env": "TEST_KEY",
                       "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 2}}}}})


@pytest.mark.parametrize("remote,approved", [(False, False), (True, False), (True, True)])
def test_s1_preview_never_calls_model_or_writes(conn, paths, monkeypatch, remote, approved):
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="Results"))
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES ('d','proj','search','done',?)", (now(),))
    conn.execute("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project) "
                 "VALUES ('d','p',1,1,1)")
    fake = FakeLlm(lambda _: {"relevance": 4, "inclusion": [], "exclusion": [], "reason": "yes"})
    monkeypatch.setattr(cli, "_make_llm", lambda *_: fake)
    monkeypatch.setattr(fake, "check", lambda: pytest.fail("preview checked a model"))
    if remote:
        cfg = cloud_config("screen")
        monkeypatch.setattr(cli, "load_config", lambda *_args, **_kw: cfg)
        monkeypatch.setattr(cli, "ModelGateway", lambda c, cfg: ModelGateway(c, cfg, providers={"remote": fake}))
    before = list(conn.iterdump())
    args = ["--data-dir", str(paths.root), "screen", "-p", "demo", "--preview"]
    args += [] if remote else ["--model", "preview-model"]
    args += ["--approve"] if approved else []
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert fake.calls == 0
    assert list(conn.iterdump()) == before
    plan = json.loads(result.output)
    assert plan["candidates"] == 1 and plan["number_of_calls"] == 1
    assert plan["models"] == (["cloud-test"] if remote else ["preview-model"])
    assert plan["remote"] is remote and plan["requires_approval"] is remote
    assert plan["max_cost_usd"] > 0 if remote else plan["max_cost_usd"] == 0


def test_ask_preview_includes_audit_task(conn, paths, monkeypatch):
    seed(conn)
    cfg = cloud_config("audit")
    previews = []
    preview = ModelGateway.preview
    def capture(self, task, *args, **kwargs):
        previews.append((task, kwargs.get("schema")))
        return preview(self, task, *args, **kwargs)
    monkeypatch.setattr(ModelGateway, "preview", capture)
    monkeypatch.setattr(cli, "load_config", lambda *_args, **_kw: cfg)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "ask", "Results", "--preview"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["remote"] and plan["requires_approval"]
    assert len([c for c in plan["calls"] if c["profile"] == "remote"]) == 8
    assert "remote" in plan["profiles"]
    assert any(task == "audit" and set(schema["properties"]) == {"verdict", "note"}
               for task, schema in previews)


def test_refresh_console_preserves_cloud_approval(conn, paths):
    seed(conn)
    matrix = create_matrix(conn, "proj", "m", "Results", [MatrixColumn(
        key="result", label="Result", instruction="Results", type="text")])
    fake = FakeLlm(lambda m: {"status": "reported", "value": "Results", "evidence_ids": [evidence_id(m)]})
    gateway = ModelGateway(conn, cloud_config("matrix_extract"), providers={"remote": fake})
    context = ActionContext(conn, gateway, "proj", "demo", paths,
                            Sources(HttpClient(transport=FakeTransport(default_routes()))), True)
    spec, args = actions.validate_action("refresh", {"screen": False, "claims": False})
    result = spec.executor(context, args)
    assert result["status"] == "ok" and fake.calls == 1
    assert conn.execute("SELECT status FROM matrix_cells WHERE matrix_id=?", (matrix.id,)).fetchone()[0] == "reported"


def test_asset_without_nearby_chunks_uses_artifact_paper(conn, paths):
    seed(conn)
    conn.execute("UPDATE chunks SET page_start=10,page_end=10")
    (paths.root / "parsed").mkdir()
    (paths.root / "parsed" / "crop.png").write_bytes(b"crop")
    conn.execute("INSERT INTO assets(id,parse_run_id,kind,label,caption,page,path,quality) "
                 "VALUES ('table','rp','table','Table 1','Parameter values',1,'parsed/crop.png','verified')")
    fake = FakeLlm(lambda _: {"columns": ["gain"], "rows": [["3.25"]], "units": {}, "footnotes": []})
    class Vision:
        def chat_json(self, messages, schema):
            return fake.chat_json(messages, schema)
        def vision_json(self, messages, schema, images):
            return fake.chat_json(messages, schema)
    gateway = ModelGateway(conn, Config(), providers={"local_vision": Vision()})
    result = figures.analyze_asset(conn, gateway, paths, "table", "table")
    assert result.evidence and all(s.paper_id == "p" for s in result.evidence)


def test_stemmed_match_cites_page_two(conn):
    seed(conn)
    text = "Results describe the setup.\nNeuronal activities increased."
    split = text.index("Neuronal")
    conn.execute("UPDATE chunks SET text=?,page_end=2,bbox_json=?", (text, json.dumps([
        {"page": 1, "start": 0, "end": split}, {"page": 2, "start": split, "end": len(text)}])))
    hits = search(conn, "activity")
    assert len(hits) == 1 and hits[0].page == 2


def abandoned_runs(conn):
    seed(conn)
    snap = snapshots.take_snapshot(conn, "proj")
    conn.execute("INSERT INTO watch_runs(id,project_id,before_snapshot,status,started_at) "
                 "VALUES ('old','proj',?,'running','2000-01-01T00:00:00+00:00')", (snap,))
    plan = planner.validate_plan({"steps": [{"action": "filter_papers", "args": {}}]}, "select")
    run = planner.create_run(conn, "proj", plan)
    conn.execute("UPDATE console_runs SET status='running',updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run,))
    return run


def test_ui_startup_repairs_abandoned_refresh_and_console(conn, paths):
    run = abandoned_runs(conn)
    server.App(paths, Config())
    old = conn.execute("SELECT status,error FROM watch_runs WHERE id='old'").fetchone()
    assert old["status"] == "error" and "interrupted" in old["error"]
    assert planner.get_run(conn, run).status == "failed"


def test_console_recovers_before_resume_rejection(conn, paths):
    run = abandoned_runs(conn)
    result = planner.execute_run(conn, ModelGateway(conn, Config()), paths, "proj", run, approved=True)
    assert result.status == "done"


class Socket:
    def __init__(self, data: bytes):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *_args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


def request(app, method, path, body=None, origin="http://evil.example"):
    payload = json.dumps(body).encode() if body is not None else b""
    sock = Socket((f"{method} {path} HTTP/1.0\r\nHost: localhost\r\nOrigin: {origin}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n").encode() + payload)
    server.make_handler(app)(sock, ("127.0.0.1", 1234), object())
    response = HTTPResponse(Socket(bytes(sock.output)))
    response.begin()
    return response.status, response.read()


@pytest.mark.parametrize("path,post_path,body", [
    ("/api/gaps?project=demo", "/api/gaps/detect", {"project": "demo"}),
    ("/api/papers/p/repro?project=demo", "/api/papers/p/repro", {"project": "demo"}),
])
def test_gap_and_repro_get_readonly_writes_require_guard(conn, paths, path, post_path, body):
    app = server.App(paths, Config())
    seed(conn, text="Results: code at https://github.com/lab/code.")
    conn.execute("INSERT INTO gap_candidates(id,project_id,kind,title,detail,source_key,created_at,updated_at) "
                 "VALUES ('g','proj','contradicted_cluster','Title','Detail','key',?,?)", (now(), now()))
    before = list(conn.iterdump())
    assert request(app, "GET", path)[0] == 200
    assert list(conn.iterdump()) == before
    assert request(app, "POST", post_path, body)[0] == 403
    assert list(conn.iterdump()) == before
    assert request(app, "POST", post_path, body, origin="http://localhost")[0] == 200
    assert list(conn.iterdump()) != before


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group liveness probe")
def test_shutdown_kills_job_helper_and_grandchild(paths, tmp_path, monkeypatch):
    pidfile = tmp_path / "grandchild.pid"
    code = ("import subprocess,sys,time,os,signal,json; "
            "p=subprocess.Popen([sys.executable,'-c','import time,signal; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)']); "
            f"open({str(pidfile)!r},'w').write(json.dumps([os.getpid(),p.pid])); time.sleep(120)")
    app = server.App(paths, Config())
    app.start_job("mineru-install", "", lambda _: _run_command(
        [sys.executable, "-c", code], env=dict(os.environ), timeout=120))
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert pidfile.exists()
    helper, grandchild = json.loads(pidfile.read_text())

    class HTTP:
        server_address = ("127.0.0.1", 0)
        def __init__(self, *_):
            pass
        def serve_forever(self):
            pass
        def shutdown(self):
            pass
        def server_close(self):
            pass

    def ctrl_c(self, timeout=None):  # Ctrl+C reaches the main thread while serve() waits
        raise KeyboardInterrupt

    monkeypatch.setattr(server.ServerHandle, "wait", ctrl_c)
    monkeypatch.setattr(server, "App", lambda *_: app)
    monkeypatch.setattr(server, "ThreadingHTTPServer", HTTP)
    monkeypatch.setattr(server, "stop_when_idle", lambda *_: None)
    try:
        server.serve(paths, Config(), port=0, open_browser=False)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(.02)
        else:
            pytest.fail("grandchild survived UI shutdown")
        with pytest.raises(ProcessLookupError):
            os.kill(helper, 0)
        assert all(job.status != "running" for job in app.jobs.values())
        assert all(not worker.is_alive() for worker, _ in app._workers.values())
    finally:
        # Keep a failing pre-fix test from leaving its reproduction alive.
        with suppress(ProcessLookupError):
            os.killpg(helper, signal.SIGKILL)


@pytest.mark.parametrize("entrypoint", ["ui", "cli"])
@pytest.mark.parametrize("owner", ["dead", "live", "expired"])
def test_startup_recovers_only_abandoned_owners(conn, paths, entrypoint, owner):
    run = abandoned_runs(conn)
    stamp = now()
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.wait()
        dead_pid = child.pid
    pid = dead_pid if owner == "dead" else os.getpid()
    started = "2000-01-01T00:00:00+00:00" if owner == "expired" else stamp
    conn.execute("UPDATE watch_runs SET started_at=?,owner_pid=?,owner_started_at=?", (stamp, pid, started))
    conn.execute("UPDATE console_runs SET updated_at=?,owner_pid=?,owner_started_at=?", (stamp, pid, started))
    if entrypoint == "ui":
        server.App(paths, Config())
    else:
        result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "snapshots", "-p", "demo"])
        assert result.exit_code == 0, result.output
    old = conn.execute("SELECT status,error FROM watch_runs WHERE id='old'").fetchone()
    console = planner.get_run(conn, run)
    if owner != "dead":
        assert old["status"] == "running" and console.status == "running"
    else:
        assert old["status"] == "error" and "interrupted" in old["error"]
        assert console.status == "failed" and "interrupted" in console.error


@pytest.mark.parametrize("identity,expected", [("same", "running"), ("reused", "failed"), (None, "running")])
def test_run_recovery_compares_process_identity(conn, monkeypatch, identity, expected):
    run = abandoned_runs(conn)
    monkeypatch.setattr(recovery, "process_started_at", lambda _: identity)
    for table in ("watch_runs", "console_runs"):
        conn.execute(f"UPDATE {table} SET owner_pid=?,owner_started_at='2000-01-01',"
                     "owner_process_started_at='same'", (os.getpid(),))
    recovery.repair_abandoned_runs(conn)
    assert planner.get_run(conn, run).status == expected
    assert conn.execute("SELECT status FROM watch_runs").fetchone()[0] == (
        "error" if expected == "failed" else "running")


@pytest.mark.parametrize("preview", ["screen", "ask", "matrix"])
def test_feature_previews_ignore_dead_reservations_on_readonly_db(conn, paths, monkeypatch, preview):
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="Results"))
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES ('d','proj','search','done',?)", (now(),))
    conn.execute("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project) "
                 "VALUES ('d','p',1,1,1)")
    mid = create_matrix(conn, "proj", "m", "Results", [MatrixColumn(
        key="result", label="Result", instruction="Results", type="text")])
    conn.execute("INSERT INTO model_call_reservations(id,cost_usd,created_at,owner_pid,owner_started_at) "
                 "VALUES ('dead',100,?,123,?)", (now(), now()))
    monkeypatch.setattr(recovery, "_alive", lambda _: False)
    before = list(conn.iterdump())
    with sqlite3.connect(f"{paths.db.as_uri()}?mode=ro", uri=True, isolation_level=None) as readonly:
        readonly.row_factory = sqlite3.Row
        gateway = ModelGateway(readonly, cloud_config("screen"))
        if preview == "screen":
            calls = preview_run(readonly, gateway, "d", 10, profile="remote")["calls"]
        elif preview == "ask":
            calls = [c.model_dump() for c in ask.plan_calls(readonly, gateway, "Results", profile="remote")]
        else:
            calls = [c.model_dump() for c in matrix.plan_calls(readonly, gateway, mid.id, profile="remote")]
        assert calls and all(c["blocked_reason"] is None for c in calls)
    assert list(conn.iterdump()) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_shutdown_deadline_shared_by_eight_helpers(paths, tmp_path, monkeypatch):
    app = server.App(paths, Config())
    monkeypatch.setattr(app, "stop_models", lambda: None)
    pidfiles = [tmp_path / f"helper-{i}.pid" for i in range(8)]
    for pidfile in pidfiles:
        code = ("import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(120)")
        app.start_job("helper", "", lambda _, code=code: _run_command(
            [sys.executable, "-c", code], env=dict(os.environ), timeout=120))
    try:
        deadline = time.monotonic() + 5
        while not all(p.exists() for p in pidfiles) and time.monotonic() < deadline:
            time.sleep(.01)
        assert all(p.exists() for p in pidfiles)
        began = time.monotonic()
        app.shutdown(timeout=.01)
        assert time.monotonic() - began < 1.5
        for worker, _ in app._workers.values():
            worker.join(2)
        assert all(not worker.is_alive() for worker, _ in app._workers.values())
        for pidfile in pidfiles:
            with pytest.raises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
    finally:
        for pidfile in pidfiles:
            if pidfile.exists():
                with suppress(ProcessLookupError):
                    os.killpg(int(pidfile.read_text()), signal.SIGKILL)


def test_refresh_recovers_before_ui_reject_check(conn, paths, monkeypatch):
    from litforge.ui.api import RefreshRequest
    app = server.App(paths, Config())
    abandoned_runs(conn)
    monkeypatch.setattr(app, "start_job", lambda kind, project, work: server.Job("job", kind, project))
    assert app.refresh_job(RefreshRequest(project="demo")).id == "job"
    assert conn.execute("SELECT status FROM watch_runs WHERE id='old'").fetchone()[0] == "error"


@pytest.mark.parametrize("entrypoint", ["cli", "ui"])
def test_refresh_interactive_cloud_approval_reaches_matrix(conn, paths, monkeypatch, entrypoint):
    from litforge.ui.api import RefreshRequest
    seed(conn)
    create_matrix(conn, "proj", "m", "Results", [MatrixColumn(
        key="result", label="Result", instruction="Results", type="text")])
    cfg = cloud_config("matrix_extract")
    fake = FakeLlm(lambda m: {"status": "reported", "value": "Results", "evidence_ids": [evidence_id(m)]})
    def gateway(c, *_):
        return ModelGateway(c, cfg, providers={"remote": fake})
    if entrypoint == "cli":
        monkeypatch.setattr(cli, "load_config", lambda *_args, **_kw: cfg)
        from litforge.analysis import living_commands
        monkeypatch.setattr(living_commands, "ModelGateway", gateway)
        result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "refresh", "-p", "demo",
                                             "--no-screen", "--no-claims", "--approve"])
        assert result.exit_code == 0, result.output
    else:
        app = server.App(paths, cfg, make_gateway=gateway)
        job = app.refresh_job(RefreshRequest(project="demo", screen=False, claims=False, approved=True))
        for worker, _ in app._workers.values():
            worker.join(5)
        assert job.status == "done", job.error
    assert fake.calls == 1
    assert conn.execute("SELECT status FROM watch_runs").fetchone()[0] == "ok"


def test_refresh_approval_reaches_screening_claims_and_relations(conn, paths):
    from litforge.analysis import claims
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="Results"))
    def reply(messages):
        if "Extract at most" in messages[0]["content"]:
            return {"claims": [{"text": "Gain improves outcomes", "type": "finding", "polarity": "positive",
                                "evidence_ids": [evidence_id(messages)]}]}
        if "Classify" in messages[0]["content"]:
            return {"relation": "replicates", "explanation": "Same"}
        return {"relevance": 5, "inclusion": [], "exclusion": [], "reason": "match"}
    fake = FakeLlm(reply)
    claims.extract_claims(conn, ModelGateway(conn, Config(), providers={"local": fake}), "proj")
    snapshots.take_snapshot(conn, "proj")
    seed(conn, "new")
    cfg = cloud_config("screen")
    for task in ("screen_fulltext", "claim_extract", "claim_relation"):
        cfg.models.tasks[task] = cfg.models.tasks["screen"].model_copy()
    gateway = ModelGateway(conn, cfg, providers={"remote": fake})
    result = living.refresh_project(conn, gateway,
        Sources(HttpClient(transport=FakeTransport(default_routes()))), "proj", approved_cloud=True)
    assert result.status == "ok"
    tasks = {r[0] for r in conn.execute("SELECT task FROM model_calls WHERE model='cloud-test'")}
    assert tasks == {"screen", "screen_fulltext", "claim_extract", "claim_relation"}


def test_shutdown_cancels_download_and_joins_before_models(paths, monkeypatch):
    app = server.App(paths, Config())
    started, cancelled = threading.Event(), threading.Event()
    events = []
    class Downloader:
        def run(self, repo, item, *, progress, control):
            started.set()
            while control() != "cancel":
                time.sleep(.01)
            events.append("download cancelled")
            cancelled.set()
            return None
    monkeypatch.setattr(app.models, "downloader", Downloader())
    monkeypatch.setattr(app, "stop_models", lambda: events.append("models stopped"))
    job = app.download_job({"confirmed": True}, plan={"repo": "test/model", "item": {"path": "model.gguf"},
                                                   "mmproj": None, "fit": {"badge": "fits"}})
    assert started.wait(2)
    app.shutdown()
    assert cancelled.is_set() and job.status == "failed"
    assert events == ["download cancelled", "models stopped"]
    assert all(not worker.is_alive() for worker, _ in app._workers.values())


def test_shutdown_bounds_worker_join(paths, monkeypatch):
    app = server.App(paths, Config())
    started, release = threading.Event(), threading.Event()
    def work(_job):
        started.set()
        release.wait(5)
    app.start_job("waiting", "", work)
    assert started.wait(2)
    monkeypatch.setattr(app, "stop_models", lambda: None)
    try:
        began = time.monotonic()
        app.shutdown(timeout=.01)
        assert time.monotonic() - began < 1
    finally:
        release.set()
        for worker, _ in app._workers.values():
            worker.join(2)


def test_s1_preview_does_not_repair_runs_or_refresh_price_cache(conn, paths, monkeypatch):
    from litforge.llm.pricing import OpenRouterPricing
    abandoned_runs(conn)
    save_criteria(conn, "proj", Criteria(question="Results"))
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES ('d','proj','search','done',?)", (now(),))
    conn.execute("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project) "
                 "VALUES ('d','p',1,1,1)")
    cfg = cloud_config("screen")
    cfg.models.profiles["remote"].pricing_source = "openrouter"
    fetched = []
    def fetch():
        fetched.append(True)
        return {"data": []}
    pricing = OpenRouterPricing(paths.http_cache, fetch=fetch)
    monkeypatch.setattr(cli, "load_config", lambda *_args, **_kw: cfg)
    monkeypatch.setattr(cli, "ModelGateway", lambda c, cfg: ModelGateway(c, cfg, pricing=pricing))
    before = list(conn.iterdump())
    files = {p.relative_to(paths.root) for p in paths.root.rglob("*") if p.is_file()}
    result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "screen", "-p", "demo", "--preview"])
    assert result.exit_code == 0, result.output
    assert list(conn.iterdump()) == before
    assert not fetched and not pricing.path.exists()
    assert {p.relative_to(paths.root) for p in paths.root.rglob("*") if p.is_file()} == files
