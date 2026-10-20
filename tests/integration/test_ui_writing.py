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


def test_assist_image_and_abstract_routes(tmp_path: Path) -> None:
    import base64

    class Vision:
        def __init__(self) -> None:
            self.images: list[list[bytes]] = []

        def check(self) -> None:
            pass

        def vision_json(self, messages, schema, images, **_):
            self.images.append(images)
            return {"replacement": "\\[ x^2 \\]", "note": "one equation"}

    httpd, base, fakes = _assist_server(tmp_path)
    fakes["local_vision"] = Vision()  # type: ignore[assignment]
    try:
        doc = create(base)
        import struct
        import zlib

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) +
               chunk(b"IDAT", zlib.compress(b"\0\xff\xff\xff")) + chunk(b"IEND", b""))
        body = {"doc": doc["id"], "action": "equation-image", "file": "main.tex", "pad_before": True,
                "image": "data:image/png;base64," + base64.b64encode(png).decode()}
        status, job = call(base, "/api/write/assist", body)
        assert status == 200 and job["sent"]["images"] == 1 and job["preview"]["model"] == "qwen2.5vl:7b"
        report = wait_job(base, job["id"])["report"]
        assert report["hunks"][0]["new"] == "\n\\[\n  x^2\n\\]" and fakes["local_vision"].images == [[png]]
        assert call(base, "/api/write/assist", {**body, "image": "data:image/png;base64,???"})[0] == 400
        status, job = call(base, "/api/write/assist", {"doc": doc["id"], "action": "abstract", "text": "",
                                                       "context": "We studied a model.", "file": "main.tex"})
        assert status == 200 and job["sent"]["characters"] == len("We studied a model.")
        assert wait_job(base, job["id"])["report"]["task"] == "write_summary"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_json_body_size_is_bounded_before_reading(tmp_path: Path, monkeypatch) -> None:
    import litforge.ui.server as server_module

    monkeypatch.setattr(server_module, "MAX_JSON_BODY", 2000)
    httpd, base, _ = _assist_server(tmp_path)
    try:
        doc = create(base)
        status, res = call(base, "/api/write/assist", {"doc": doc["id"], "action": "equation-image",
                                                       "image": "A" * 3000})
        assert status == 413 and "too large" in res["error"]
        save = {"doc": doc["id"], "name": "main.tex", "text": "ok", "hash": None}
        assert call(base, "/api/write/save", save)[0] != 413
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


def test_only_one_question_at_a_time_even_when_sent_together(tmp_path: Path) -> None:
    import threading as _threading
    import time as _time

    httpd, base, fakes = _assist_server(tmp_path)
    try:
        doc = create(base)
        fakes["local"].answer = lambda m: (_time.sleep(0.5), {"answer": "ok", "citations": []})[1]
        body = {"doc": doc["id"], "question": "Q?", "file": "main.tex"}
        statuses: list[int] = []
        threads = [_threading.Thread(target=lambda: statuses.append(call(base, "/api/write/chat", body)[0]))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(statuses).count(200) == 1, statuses
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_settings_route_and_outputs_follow_the_main_file(server) -> None:
    base, app = server
    doc = create(base)
    folder = Path(app.paths.project_dir("demo")) / "writing" / doc["slug"]
    (folder / "paper.tex").write_text("\\documentclass{article}\n\\begin{document}x\\end{document}\n")
    status, info = call(base, f"/api/write/doc?doc={doc['id']}")
    assert info["main_file"] == "main.tex" and info["main_candidates"] == ["main.tex", "paper.tex"]
    status, changed = call(base, "/api/write/settings", {"doc": doc["id"], "main_file": "paper.tex", "draft": True,
                                                         "engine": "xelatex"})
    assert status == 200
    assert (changed["main_file"], changed["draft"], changed["engine"]) == ("paper.tex", True, "xelatex")
    assert call(base, "/api/write/settings", {"doc": doc["id"], "main_file": "../x.tex"})[0] == 400
    (folder / ".build").mkdir(exist_ok=True)
    (folder / ".build" / "paper.pdf").write_bytes(b"%PDF-1.5 paper")
    assert call(base, f"/api/write/doc?doc={doc['id']}")[1]["pdf"] is True
    status, pdf = call(base, f"/api/write/pdf?doc={doc['id']}")
    assert status == 200 and pdf == b"%PDF-1.5 paper"
    status, res = call(base, f"/api/write/synctex?doc={doc['id']}&file=paper.tex&line=1")
    assert status == 404 and "compile" in res["error"]


def test_archive_routes(server):
    import io
    import zipfile
    from urllib.request import urlopen

    base, _ = server
    doc = create(base)
    with urlopen(base + f"/api/write/export.zip?doc={doc['id']}") as response:
        assert response.headers["Content-Type"] == "application/zip"
        assert response.headers["Content-Disposition"] == 'attachment; filename="draft.zip"'
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "main.tex" in archive.namelist()
    status, imported = call(base, "/api/write/import?project=demo&name=Imported", raw=data)
    assert status == 200, imported
    assert imported["template"] == "imported" and imported["name"] == "Imported"
    assert imported["id"] != doc["id"]
    assert call(base, f"/api/write/file?doc={imported['id']}&name=main.tex")[1]["text"] == call(
        base, f"/api/write/file?doc={doc['id']}&name=main.tex")[1]["text"]
    assert call(base, "/api/write/import?project=demo&name=Bad", raw=b"bad")[0] == 400
    for headers in ({"Origin": "http://evil.example"}, {"Host": "evil.example"}):
        assert call(base, "/api/write/import?project=demo&name=Bad", raw=data, headers=headers)[0] == 403


def test_archive_import_size_limit_before_read(server):
    import http.client
    from urllib.parse import urlsplit

    base, _ = server
    url = urlsplit(base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
    try:
        conn.request("POST", "/api/write/import?project=demo&name=Big", body=None,
                     headers={"Content-Length": str(100 * 1024 * 1024 + 1)})
        response = conn.getresponse()
        assert response.status in {400, 413}
        assert b"100 MB" in response.read()
    finally:
        conn.close()


def test_archive_handler_without_socket(tmp_path):
    import io
    import json
    import zipfile
    from types import SimpleNamespace

    app = App(Paths(tmp_path / "data", exports_root=tmp_path / "exports"), load_config())
    conn = app.db()
    create_project(conn, app.paths, "demo")
    conn.close()

    class Request:
        def __init__(self, data):
            self.input = io.BytesIO(data)
            self.output = bytearray()

        def makefile(self, *args):
            return self.input

        def sendall(self, data):
            self.output.extend(data)

    def request(method, path, body=b"", headers=None):
        fields = {"Host": "localhost", "Content-Length": str(len(body)), **(headers or {})}
        raw = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items())
        req = Request(raw.encode() + b"\r\n" + body)
        make_handler(app)(req, ("127.0.0.1", 1234), SimpleNamespace(server_address=("127.0.0.1", 1234)))
        head, payload = bytes(req.output).split(b"\r\n\r\n", 1)
        return head, payload

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("main.tex", r"\documentclass{article}")
    data = out.getvalue()
    head, payload = request("POST", "/api/write/import?project=demo&name=Paper", data)
    assert b"200 OK" in head, payload
    doc = json.loads(payload)
    head, payload = request("GET", f"/api/write/export.zip?doc={doc['id']}")
    assert b"200 OK" in head and b"Content-Type: application/zip" in head
    assert b'Content-Disposition: attachment; filename="paper.zip"' in head
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert z.read("main.tex") == b"\\documentclass{article}"
    for headers in ({"Origin": "http://evil.example"}, {"Host": "evil.example"}):
        head, _ = request("POST", "/api/write/import?project=demo&name=Bad", data, headers)
        assert b"403" in head
    head, payload = request("POST", "/api/write/import?project=demo&name=Big",
                            headers={"Content-Length": str(100 * 1024 * 1024 + 1)})
    assert b"400" in head and b"100 MB" in payload
    head, payload = request("POST", "/api/write/import?project=demo&name=Bad", b"bad")
    assert b"400" in head and b"ZIP" in payload


def test_settings_wait_for_a_running_compile(server) -> None:
    from litforge.ui.server import Job

    base, app = server
    doc = create(base)
    app.jobs["busy"] = Job(id="busy", kind="writing-compile", project=doc["id"], status="running")
    try:
        status, res = call(base, "/api/write/settings", {"doc": doc["id"], "draft": True})
        assert status == 400 and "compil" in res["error"]
    finally:
        app.jobs.pop("busy")
    assert call(base, "/api/write/settings", {"doc": doc["id"], "draft": True})[0] == 200


def test_writing_aids_handler_without_socket(tmp_path, monkeypatch):
    import io
    import json
    from types import SimpleNamespace
    from urllib.parse import urlencode

    from tests.unit.test_writing_compile import ERROR_LOG, FAKE

    app = App(Paths(tmp_path / 'data', exports_root=tmp_path / 'exports'), load_config())
    conn = app.db()
    create_project(conn, app.paths, 'demo')
    conn.close()
    engine = app.paths.root / 'tools' / 'tectonic' / 'tectonic'
    engine.parent.mkdir(parents=True)
    engine.write_text(FAKE.format(python=sys.executable, record=str(tmp_path / 'record'),
                                  mode=str(tmp_path / 'mode'), log=ERROR_LOG))
    engine.chmod(0o755)
    monkeypatch.setattr(wc, 'sandbox_kind', lambda: 'scan')

    class Request:
        def __init__(self, raw):
            self.input, self.output = io.BytesIO(raw), bytearray()

        def makefile(self, *args):
            return self.input

        def sendall(self, data):
            self.output.extend(data)

    def request(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else b''
        raw = (f'{method} {path} HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\n'
               f'Content-Length: {len(data)}\r\n\r\n').encode() + data
        req = Request(raw)
        make_handler(app)(req, ('127.0.0.1', 1234), SimpleNamespace(server_address=('127.0.0.1', 1234)))
        head, payload = bytes(req.output).split(b'\r\n\r\n', 1)
        return int(head.split()[1]), head, payload

    status, _, payload = request('POST', '/api/write/create', {'project': 'demo', 'name': 'Draft'})
    assert status == 200
    doc = json.loads(payload)
    folder = app.paths.project_dir('demo') / 'writing' / doc['slug']
    body = {'doc': doc['id']}
    status, head, pdf = request('POST', '/api/write/preview', {**body, 'tex': '$x$'})
    assert status == 200 and pdf.startswith(b'%PDF')
    assert b'Content-Type: application/pdf' in head and b'Content-Disposition: inline' in head
    assert request('POST', '/api/write/preview', {**body, 'tex': r'$\input{x}$'})[0] == 400
    for name, content_type in [('x.pdf', 'application/pdf'), ('x.png', 'image/png'),
                               ('x.jpg', 'image/jpeg'), ('x.jpeg', 'image/jpeg')]:
        (folder / name).write_bytes(name.encode())
        status, head, data = request('GET', '/api/write/figure?' + urlencode({**body, 'name': name}))
        assert status == 200 and data == name.encode() and content_type.encode() in head
        assert b'Content-Disposition: inline' in head
    assert request('GET', '/api/write/figure?' + urlencode({**body, 'name': 'x'}))[2] == b'x.pdf'
    (folder / 'x.pdf').unlink()
    assert request('GET', '/api/write/figure?' + urlencode({**body, 'name': 'x'}))[2] == b'x.png'
    (folder / '.hidden.png').write_bytes(b'x')
    (folder / 'x.svg').write_bytes(b'<svg/>')
    (folder / 'link.png').symlink_to(tmp_path / 'outside.png')
    for name, expected in [('../x.png', 400), ('.hidden.png', 400), ('x.svg', 400),
                           ('link.png', 400), ('missing.png', 404), ('missing', 404)]:
        assert request('GET', '/api/write/figure?' + urlencode({**body, 'name': name}))[0] == expected
    with (folder / 'big.png').open('wb') as stream:
        stream.truncate(25 * 1024 * 1024 + 1)
    assert request('GET', '/api/write/figure?' + urlencode({**body, 'name': 'big.png'}))[0] == 400
    (folder / 'main.tex').write_text('one\ntwo\nthree')
    status, _, payload = request('POST', '/api/write/note', {**body, 'file': 'main.tex', 'line': 2,
                                                          'anchor': 'two', 'body': ' note '})
    assert status == 200
    note = json.loads(payload)
    assert note['body'] == 'note'
    (folder / 'main.tex').write_text('two\none\nthree')
    query = urlencode({**body, 'file': 'main.tex'})
    listing = json.loads(request('GET', '/api/write/notes?' + query)[2])['notes']
    assert listing[0]['line'] == 1 and listing[0]['moved']
    status, _, payload = request('POST', '/api/write/note', {**body, **note, 'line': 3, 'anchor': 'three',
                                                          'body': 'edited'})
    assert status == 200 and json.loads(payload)['body'] == 'edited'
    moved = request('POST', '/api/write/notes/move', {**body, 'file': 'main.tex',
                                                    'notes': [{'id': note['id'], 'line': 2, 'anchor': 'one'}]})
    assert moved[0] == 200 and json.loads(moved[2]) == {'moved': 1}
    assert request('POST', '/api/write/note', {**body, 'file': 'main.tex', 'line': 1, 'body': ''})[0] == 400
    assert request('POST', '/api/write/note/delete', {**body, 'id': note['id']})[0] == 200
    assert request('POST', '/api/write/note/delete', {**body, 'id': note['id']})[0] == 404


def _git_remote(tmp_path):
    import shutil
    import subprocess

    if not shutil.which('git'):
        pytest.skip('git is not installed')
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(remote)], check=True,
                   capture_output=True)
    return str(remote)


def test_git_http_routes(server, tmp_path):
    base, _ = server
    remote = _git_remote(tmp_path)
    doc = create(base)
    body = {'doc': doc['id']}
    assert call(base, f"/api/write/git?doc={doc['id']}")[1]['repo'] is False
    code, result = call(base, '/api/write/git/connect', {**body, 'url': remote})
    assert code == 200 and result['remote'] == remote
    _, job = call(base, '/api/write/git/sync', body)
    done = wait_job(base, job['id'])
    assert done['status'] == 'done' and done['report']['pushed'] == 1
    _, job = call(base, '/api/write/git/clone', {'project': 'demo', 'name': 'Clone', 'url': remote})
    done = wait_job(base, job['id'])
    assert done['status'] == 'done' and done['report']['template'] == 'git'
    assert call(base, '/api/write/git/abort', body)[0] == 400
    assert call(base, '/api/write/git/disconnect', body)[1]['remote'] is None


def test_git_routes_without_sockets(tmp_path, monkeypatch):
    import time

    from litforge.ui import writing_routes as routes
    from litforge.ui.server import Job
    from litforge.writing import documents as wd
    from litforge.writing import gitsync as gs

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / '.gitconfig'))
    remote = _git_remote(tmp_path)
    paths = Paths(tmp_path / 'data', exports_root=tmp_path / 'exports')
    app = App(paths, load_config())
    conn = app.db()
    create_project(conn, paths, 'demo')
    doc = wd.create_document(conn, paths, 'demo', 'Paper')
    body = {'doc': doc.id}

    def post(action, extra=None):
        return routes.post(app, conn, '/api/write/git/' + action, {**body, **(extra or {})})

    def done(job):
        deadline = time.monotonic() + 10
        while app.jobs[job['id']].status == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        result = app.jobs[job['id']]
        assert result.status == 'done', result.error
        return result.report

    try:
        assert not routes.get(app, conn, '/api/write/git', body)['repo']
        assert post('connect', {'url': remote})['repo']
        assert done(post('sync'))['pushed'] == 1
        cloned = done(routes.post(app, conn, '/api/write/git/clone',
                                 {'project': 'demo', 'name': 'Clone', 'url': remote}))
        assert cloned['template'] == 'git'
        other = wd.get_document(conn, paths, cloned['id'])
        (doc.folder / 'main.tex').write_text('local\n')
        (other.folder / 'main.tex').write_text('remote\n')
        gs.sync(other)
        assert done(post('sync'))['conflicts'] == ['main.tex']
        assert not post('abort')['merging']
        assert post('disconnect')['remote'] is None
        for kind, message in [('writing-compile', 'compile'), ('writing-git', 'already running')]:
            busy = Job(id='busy', kind=kind, project=doc.id)
            app.jobs[busy.id] = busy
            for action in ('connect', 'sync', 'abort', 'disconnect'):
                with pytest.raises(ValueError, match=message):
                    post(action, {'url': remote})
            if kind == 'writing-git':
                with pytest.raises(ValueError, match='git operation'):
                    routes.compile_job(app, doc)
            del app.jobs[busy.id]
    finally:
        conn.close()
        app.shutdown()
