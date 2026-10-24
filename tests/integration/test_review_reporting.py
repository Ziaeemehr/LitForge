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
