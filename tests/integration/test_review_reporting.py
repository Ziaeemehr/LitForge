"""Exercise the real HTTP handler over in-memory connections (no listening socket)."""
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from litforge.config import load_config
from litforge.projects import create_project
from litforge.ui.server import App, make_handler


class Connection:
    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.response = bytearray()

    def makefile(self, mode, buffering):
        return self.request

    def sendall(self, data):
        self.response.extend(data)


def call(app, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    method = "POST" if body is not None else "GET"
    request = (f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n"
               f"Content-Length: {len(data)}\r\n\r\n").encode() + data
    conn = Connection(request)
    make_handler(app)(conn, ("127.0.0.1", 1234), None)
    headers, payload = bytes(conn.response).split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), json.loads(payload)


def test_prisma_routes(paths):
    app = App(paths, load_config())
    conn = app.db()
    create_project(conn, paths, "Review")
    conn.close()
    status, report = call(app, "/api/review/prisma?project=Review")
    assert status == 200
    assert report["counts"]["included"] == 0
    ET.fromstring(report["svg"])
    status, saved = call(app, "/api/review/prisma/save", {"project": "Review"})
    assert status == 200
    assert Path(saved["path"]).name == "prisma.svg"
    assert Path(saved["path"]).read_text() == report["svg"]
    assert call(app, "/api/review/prisma?project=missing")[0] == 404
    assert call(app, "/api/review/missing?project=Review")[0] == 404
    assert call(app, "/api/review/prisma/save", {"project": "missing"})[0] == 400


def test_reference_routes(paths):
    app = App(paths, load_config())
    conn = app.db()
    pid = create_project(conn, paths, "References")
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('a','A study','now','now')")
    conn.execute("INSERT INTO project_papers VALUES (?,'a','import','now')", (pid,))
    conn.execute("INSERT INTO paper_marks VALUES (?,'a','relevant','now')", (pid,))
    conn.close()
    for fmt in ("ris", "bib"):
        status, saved = call(app, "/api/review/references", {
            "project": "References", "format": fmt, "status": ["included"]})
        assert status == 200
        assert saved["count"] == 1
        assert Path(saved["path"]).name == f"references.{fmt}"
        assert "A study" in Path(saved["path"]).read_text()
    status, saved = call(app, "/api/review/references", {
        "project": "References", "format": "ris", "status": ["excluded"]})
    assert status == 200 and saved["count"] == 0
    assert Path(saved["path"]).read_text() == ""
    for extra in ({"format": "pdf"}, {"status": ["include"]}, {"status": "included"}):
        assert call(app, "/api/review/references", {"project": "References", "format": "ris", **extra})[0] == 400
    assert call(app, "/api/review/references", {"project": "missing", "format": "ris"})[0] == 400
