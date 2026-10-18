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
