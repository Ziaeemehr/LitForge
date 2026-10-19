"""Writing Studio HTTP routes against the real handler, with a fake LaTeX engine."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_ui_server import call, wait_job

from litforge.config import Paths, load_config
from litforge.projects import create_project
from litforge.ui.server import App, make_handler
from litforge.writing import compile as wc

FAKE_ENGINE = """#!{python}
from pathlib import Path
text = Path("main.tex").read_text()
if "BROKEN" in text:
    Path(".build/main.log").write_text("(./main.tex\\n! Undefined control sequence.\\nl.3 BROKEN\\n)")
    raise SystemExit(1)
Path(".build/main.log").write_text("(./main.tex)")
Path(".build/main.pdf").write_bytes(b"%PDF-1.5 fake")
"""


@pytest.fixture()
def server(tmp_path: Path, monkeypatch) -> Iterator[tuple[str, App]]:
    from http.server import ThreadingHTTPServer

    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    app = App(paths, load_config())
    conn = app.db()
    create_project(conn, paths, "demo")
    conn.close()
    engine = paths.root / "tools" / "tectonic" / "tectonic"
    engine.parent.mkdir(parents=True)
    engine.write_text(FAKE_ENGINE.format(python=sys.executable))
    engine.chmod(0o755)
    monkeypatch.setattr(wc, "sandbox_kind", lambda: "scan")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()
    httpd.server_close()


def create(base: str, template: str = "article") -> dict[str, Any]:
    status, doc = call(base, "/api/write/create", {"project": "demo", "name": "Draft", "template": template})
    assert status == 200, doc
    return doc  # type: ignore[no-any-return]


@pytest.mark.skipif(os.name == "nt", reason="fake engine is a POSIX script")
def test_create_edit_compile_and_pdf(server) -> None:
    base, _ = server
    doc = create(base)
    status, listing = call(base, "/api/write/docs?project=demo")
    assert status == 200 and [d["id"] for d in listing["documents"]] == [doc["id"]]
    assert set(listing["templates"]) == {"article", "persian"}
    _, file = call(base, f"/api/write/file?doc={doc['id']}&name=main.tex")
    assert "\\documentclass" in file["text"]
    status, saved = call(base, "/api/write/save", {"doc": doc["id"], "name": "main.tex",
                                                   "text": file["text"] + "\n% edit", "hash": file["hash"]})
    assert status == 200 and saved["hash"] != file["hash"]
    status, conflict = call(base, "/api/write/save", {"doc": doc["id"], "name": "main.tex", "text": "other",
                                                      "hash": file["hash"]})
    assert status == 409 and conflict["conflict"] and conflict["hash"] == saved["hash"]
    assert call(base, f"/api/write/pdf?doc={doc['id']}")[0] == 404
    _, job = call(base, "/api/write/compile", {"doc": doc["id"]})
    done = wait_job(base, job["id"])
    assert done["status"] == "done" and done["report"]["ok"], done
    status, pdf = call(base, f"/api/write/pdf?doc={doc['id']}")
    assert status == 200 and pdf.startswith(b"%PDF")
    _, snaps = call(base, f"/api/write/snapshots?doc={doc['id']}")
    assert len(snaps["snapshots"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="fake engine is a POSIX script")
def test_compile_errors_reach_the_page(server) -> None:
    base, _ = server
    doc = create(base)
    _, file = call(base, f"/api/write/file?doc={doc['id']}&name=main.tex")
    call(base, "/api/write/save", {"doc": doc["id"], "name": "main.tex", "text": "a\nb\nBROKEN\n",
                                   "hash": file["hash"]})
    _, job = call(base, "/api/write/compile", {"doc": doc["id"]})
    report = wait_job(base, job["id"])["report"]
    assert not report["ok"] and report["errors"][0]["line"] == 3


def test_cross_origin_posts_refused(server) -> None:
    base, _ = server
    for path in ("/api/write/create", "/api/write/save", "/api/write/compile", "/api/write/upload?doc=x&name=a.png"):
        status, _ = call(base, path, {"project": "demo", "name": "x"}, headers={"Origin": "http://evil.example"})
        assert status == 403, path


@pytest.mark.parametrize("name", ["../../../settings.json", "/etc/passwd", ".build/main.log", "%2e%2e/x.tex"])
def test_traversal_attempts(server, name: str) -> None:
    base, app = server
    doc = create(base)
    from urllib.parse import quote

    status, _ = call(base, f"/api/write/file?doc={doc['id']}&name={quote(name)}")
    assert status in (400, 404)
    status, _ = call(base, "/api/write/save", {"doc": doc["id"], "name": name, "text": "x", "hash": None})
    folder = app.paths.project_dir("demo") / "writing" / "draft"
    # A literal "%2e%2e" folder name is legal and stays inside the document; the others are refused.
    assert status == (200 if name.startswith("%") else 400)
    assert not (app.paths.root / "x.tex").exists() and not (folder.parent / "x.tex").exists()


def test_upload_figure_and_delete(server) -> None:
    base, app = server
    doc = create(base, "persian")
    status, up = call(base, f"/api/write/upload?doc={doc['id']}&name=../plot.png", raw=b"\x89PNG data")
    assert status == 200 and up["name"] == "figures/plot.png"
    status, _ = call(base, f"/api/write/upload?doc={doc['id']}&name=run.sh", raw=b"#!/bin/sh")
    assert status == 400
    _, info = call(base, f"/api/write/doc?doc={doc['id']}")
    assert "figures/plot.png" in {f["name"] for f in info["files"]}
    assert call(base, "/api/write/delete", {"doc": doc["id"]})[0] == 200
    assert call(base, f"/api/write/doc?doc={doc['id']}")[0] == 404
    assert any((app.paths.project_dir("demo") / "trash").iterdir())


def test_tools_status_lists_tectonic(server) -> None:
    base, _ = server
    _, tools = call(base, "/api/tools")
    assert tools["tectonic"]["tectonic"]["installed"] is True
    assert call(base, "/api/tools/tectonic/install", {})[0] == 400  # size not confirmed


def test_cite_routes_and_bib_sync(server) -> None:
    base, app = server
    conn = app.db()
    pid = conn.execute("SELECT id FROM projects WHERE name='demo'").fetchone()[0]
    conn.execute("""INSERT INTO papers(id,title,year,authors_json,created_at,updated_at)
                    VALUES ('p1','Whole brain models',2020,'["Ann Lee"]','now','now')""")
    conn.execute("INSERT INTO project_papers(project_id,paper_id,source,added_at) VALUES (?,'p1','manual','now')",
                 (pid,))
    conn.close()
    doc = create(base)
    status, found = call(base, f"/api/write/cite/search?doc={doc['id']}&q=brain")
    assert status == 200 and [r["paper_id"] for r in found["results"]] == ["p1"]
    status, cited = call(base, "/api/write/cite", {"doc": doc["id"], "paper_id": "p1"})
    assert status == 200 and cited["key"] == "lee2020whole"
    _, bib = call(base, f"/api/write/file?doc={doc['id']}&name=references.bib")
    assert "@misc{lee2020whole," in bib["text"]
    status, quotes = call(base, f"/api/write/quote?doc={doc['id']}&key=lee2020whole")
    assert status == 200 and quotes["quotes"] == []
    assert call(base, f"/api/write/quote?doc={doc['id']}&key=nokey")[0] == 404
    assert call(base, "/api/write/cite", {"doc": doc["id"], "paper_id": "p1"},
                headers={"Origin": "http://evil.example"})[0] == 403


def _assist_server(tmp_path: Path, config_edit=None):
    from http.server import ThreadingHTTPServer

    from litforge.analysis.gateway import ModelGateway
    from litforge.screening.llm import FakeLlm

    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    config = load_config()
    if config_edit:
        config_edit(config)
    fakes = {"local": FakeLlm(lambda m: {"replacement": "Short text.", "note": "shorter"}),
             "remote": FakeLlm(lambda m: {"replacement": "Cloud text."})}
    app = App(paths, config, make_gateway=lambda conn: ModelGateway(conn, config, providers=fakes))
    conn = app.db()
    create_project(conn, paths, "demo")
    conn.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", fakes


def test_assist_local_job_and_decide(tmp_path: Path) -> None:
    httpd, base, fakes = _assist_server(tmp_path)
    try:
        doc = create(base)
        status, job = call(base, "/api/write/assist", {"doc": doc["id"], "action": "shorter",
                                                       "text": "A rather long piece of text.", "file": "main.tex"})
        assert status == 200 and job["preview"]["remote"] is False and job["sent"]["characters"] == 28
        report = wait_job(base, job["id"])["report"]
        assert report["hunks"] and report["original"] == "A rather long piece of text."
        status, decided = call(base, "/api/write/assist/decide", {"doc": doc["id"], "suggestion": report["id"],
                                                                  "accepted": [0]})
        assert status == 200 and decided["status"] == "accepted"
        assert call(base, "/api/write/assist", {"doc": doc["id"], "action": "shorter", "text": "x",
                                                "file": "../x.tex"})[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_assist_cloud_route_needs_approval(tmp_path: Path) -> None:
    from litforge.config import ModelProfileConfig, TaskModelConfig
    from litforge.llm.budget import Pricing

    def cloud(config) -> None:
        config.models.policy = "ask_before_cloud"
        config.privacy.allow_cloud = True
        config.models.profiles["remote"] = ModelProfileConfig(
            provider="openai_compatible", model="big", remote=True,
            pricing=Pricing(input_per_million_usd=1, output_per_million_usd=2))
        config.models.tasks["write_rewrite"] = TaskModelConfig(profile="remote")

    httpd, base, fakes = _assist_server(tmp_path, cloud)
    try:
        doc = create(base)
        body = {"doc": doc["id"], "action": "clearer", "text": "Some text.", "file": "main.tex"}
        status, res = call(base, "/api/write/assist", body)
        assert status == 200 and res["needs_approval"] and res["preview"]["model"] == "big"
        assert fakes["remote"].calls == 0
        _, job = call(base, "/api/write/assist", {**body, "approved": True})
        assert wait_job(base, job["id"])["status"] == "done" and fakes["remote"].calls == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_dns_rebinding_host_refused(server) -> None:
    base, _ = server
    doc = create(base)
    assert call(base, f"/api/write/file?doc={doc['id']}&name=main.tex", headers={"Host": "evil.example"})[0] == 403
    assert call(base, "/api/write/save", {"doc": doc["id"], "name": "main.tex", "text": "x", "hash": None},
                headers={"Host": "evil.example:80", "Origin": "http://evil.example:80"})[0] == 403


def test_synctex_lookups_both_ways(server) -> None:
    import gzip

    base, app = server
    doc = create(base)
    folder = Path(app.paths.root) / "projects" / "demo" / "writing" / "draft"
    fixture = Path(__file__).parents[1] / "fixtures" / "synctex" / "en.synctex.gz"
    (folder / ".build").mkdir(exist_ok=True)
    data = gzip.decompress(fixture.read_bytes()).replace(b"/doc/", str(folder.resolve()).encode() + b"/")
    assert call(base, f"/api/write/synctex?doc={doc['id']}&file=main.tex&line=4")[0] == 404  # not compiled yet
    (folder / ".build" / "main.synctex.gz").write_bytes(gzip.compress(data))
    status, hit = call(base, f"/api/write/synctex?doc={doc['id']}&file=main.tex&line=4")
    assert status == 200 and hit["page"] == 1 and hit["boxes"]
    status, back = call(base, f"/api/write/synctex?doc={doc['id']}&page=1&x=145&y=153")
    assert status == 200 and back == {"file": "main.tex", "line": 4}
    assert call(base, f"/api/write/synctex?doc={doc['id']}&page=1&x=abc&y=1")[0] == 400
    assert call(base, f"/api/write/synctex?doc={doc['id']}&file=../../x&line=1")[0] in (400, 404)


def test_synctex_being_written_gives_a_clear_404(server) -> None:
    import gzip

    base, app = server
    doc = create(base)
    build = Path(app.paths.root) / "projects" / "demo" / "writing" / "draft" / ".build"
    build.mkdir(exist_ok=True)
    (build / "main.synctex.gz").write_bytes(gzip.compress(b"SyncTeX Version:1\n" * 500)[:50])
    status, body = call(base, f"/api/write/synctex?doc={doc['id']}&file=main.tex&line=1")
    assert status == 404 and "compile" in body["error"]


def test_version_history_routes(server) -> None:
    base, _ = server
    doc = create(base)
    body = {"doc": doc["id"], "note": "checkpoint", "name": " Draft "}
    status, snap = call(base, "/api/write/snapshot", body)
    assert status == 200
    status, listing = call(base, f"/api/write/snapshots?doc={doc['id']}")
    assert status == 200
    item = listing["snapshots"][0]
    assert set(item) == {"id", "created_at", "note", "name", "files", "added", "removed"}
    assert item["name"] == "Draft" and item["added"] > 0 and item["removed"] == 0
    status, renamed = call(base, "/api/write/snapshot/name", {"doc": doc["id"], "snapshot": snap["id"],
                                                              "name": " Final "})
    assert status == 200 and renamed == {"id": snap["id"], "name": "Final"}
    status, diff = call(base, f"/api/write/diff?doc={doc['id']}&a={snap['id']}&b=current&file=main.tex")
    assert status == 200 and diff["hunks"] == [] and diff["file"] == "main.tex"
    assert call(base, f"/api/write/diff?doc={doc['id']}")[1]["hunks"] == []
    assert call(base, f"/api/write/diff?doc={doc['id']}&a=bad")[0] == 400
    assert call(base, "/api/write/snapshot/name", {"doc": doc["id"], "snapshot": "../bad"})[0] == 400
    for path in ("/api/write/snapshot", "/api/write/snapshot/name"):
        assert call(base, path, body, headers={"Origin": "http://evil.example"})[0] == 403
        assert call(base, path, {"doc": "unknown"})[0] == 404
    for path in ("snapshots", "diff"):
        assert call(base, f"/api/write/{path}?doc=unknown")[0] == 404
    second = call(base, "/api/write/snapshot", body)[1]
    assert second["id"] != snap["id"]


def test_chat_local_job_and_cloud_approval(tmp_path: Path) -> None:
    from litforge.config import ModelProfileConfig, TaskModelConfig
    from litforge.llm.budget import Pricing

    def cloud(config) -> None:
        config.models.policy = "ask_before_cloud"
        config.privacy.allow_cloud = True
        config.models.profiles["remote"] = ModelProfileConfig(
            provider="openai_compatible", model="big", remote=True,
            pricing=Pricing(input_per_million_usd=1, output_per_million_usd=2))
        config.models.tasks["write_chat"] = TaskModelConfig(profile="remote")

    httpd, base, fakes = _assist_server(tmp_path)
    try:
        doc = create(base)
        fakes["local"].answer = lambda m: {"answer": "Use tabular.", "citations": []}
        body = {"doc": doc["id"], "question": "How do I make a table?", "selection": "", "context": "x",
                "history": [], "file": "main.tex"}
        status, job = call(base, "/api/write/chat", body)
        assert status == 200 and job["preview"]["remote"] is False, job
        report = wait_job(base, job["id"])["report"]
        assert report["answer"] == "Use tabular." and report["suggestion"] is None
        assert call(base, "/api/write/chat", {**body, "question": ""})[0] == 400
        assert call(base, "/api/write/chat", body, headers={"Origin": "http://evil.example"})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
    httpd, base, fakes = _assist_server(tmp_path / "cloud", cloud)
    try:
        doc = create(base)
        body = {"doc": doc["id"], "question": "Shorter?", "selection": "Some text.", "file": "main.tex"}
        status, res = call(base, "/api/write/chat", body)
        assert status == 200 and res["needs_approval"] and res["preview"]["model"] == "big"
        assert fakes["remote"].calls == 0
        fakes["remote"].answer = lambda m: {"answer": "Done.", "citations": [], "replacement": "Text."}
        _, job = call(base, "/api/write/chat", {**body, "approved": True})
        done = wait_job(base, job["id"])
        assert done["status"] == "done" and fakes["remote"].calls == 1 and done["report"]["suggestion"]["hunks"]
    finally:
        httpd.shutdown()
        httpd.server_close()
