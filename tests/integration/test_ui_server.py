"""Web UI server: real HTTP on a free local port, discovery through the fake transport."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from discovery_fakes import FakeTransport, default_routes

from litforge.config import Paths, load_config
from litforge.discovery.http import HttpClient
from litforge.ui.server import App, make_handler

ELICIT_CSV = (
    "Title,Authors,URL,DOI,Published Date,Summary,Relevance\n"
    '"Feeling the Beat: Movement Influences Infant Rhythm Perception","Jessica Phillips-Silver, L. Trainor",'
    "https://www.semanticscholar.org/paper/x,10.1126/SCIENCE.1110922,2005,"
    '"We hear the melody in music, but we feel the beat.","Closest mechanistic parallel"\n'
)


@pytest.fixture()
def server(tmp_path: Path) -> Iterator[str]:
    transport = FakeTransport(default_routes())
    config = load_config()
    config.similar.embedding_model = None  # keep tests fast and offline
    app = App(Paths(tmp_path / "data"), config,
              make_http=lambda: HttpClient(transport=transport, sleep=lambda s: None, min_interval={}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base: str, path: str, body: Any = None, raw: bytes | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, json.loads(payload) if ctype.startswith("application/json") else payload
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_job(base: str, job_id: str) -> dict[str, Any]:
    for _ in range(100):
        _, job = call(base, f"/api/jobs/{job_id}")
        if job["status"] != "running":
            return job  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_page_and_assets_are_served(server: str) -> None:
    status, html = call(server, "/")
    assert status == 200 and b"LitForge" in html and b"/static/app.js" in html
    for asset in ("/static/app.js", "/static/md.js", "/static/vendor/katex.min.js", "/static/vendor/marked.min.js"):
        assert call(server, asset)[0] == 200, asset
    assert call(server, "/static/../server.py")[0] == 404


def test_similar_job_results_detail_and_marks(server: str) -> None:
    status, job = call(server, "/api/similar", {"project": "web", "seeds": ["10.1073/pnas.1912789117"],
                                                "embeddings": False})
    assert status == 200
    job = wait_job(server, job["id"])
    assert job["status"] == "done", job
    assert any(m.startswith("following citations") for m in job["messages"])
    _, data = call(server, f"/api/runs/{job['run_id']}")
    titles = [p["title"] for p in data["papers"]]
    assert titles[0] == "The frontier of simulation-based inference"
    stats = data["stats"]
    assert stats["papers"] == len(titles) and stats["year_min"] == 2019 and dict(stats["channels"])["references"] == 1
    apt = next(p for p in data["papers"] if p["title"].startswith("Automatic"))
    assert "cited by the seed" in apt["reasons"] and apt["components"]["coupling"] == 1.0

    _, detail = call(server, f"/api/papers/{apt['id']}?project=web")
    assert detail["venue"] == "ICML" and detail["abstract"] == "simulation inference"
    assert call(server, "/api/marks", {"project": "web", "paper_id": apt["id"], "mark": "relevant"})[0] == 200
    _, data = call(server, f"/api/runs/{job['run_id']}")
    assert data["stats"]["relevant"] == 1
    assert call(server, "/api/marks", {"project": "web", "paper_id": apt["id"], "mark": "bogus"})[0] == 400

    _, projects = call(server, "/api/projects")
    assert projects[0]["name"] == "web" and projects[0]["export_dir"].endswith("web/exports")
    _, runs = call(server, "/api/runs?project=web")
    assert runs[0]["kind"] == "similar" and runs[0]["label"] == "10.1073/pnas.1912789117"

    # "More like my ✓ papers" uses the relevant paper as seed.
    _, job2 = call(server, "/api/similar", {"project": "web", "from_relevant": True, "embeddings": False})
    assert wait_job(server, job2["id"])["status"] == "done"


def test_import_elicit_then_expand(server: str, tmp_path: Path) -> None:
    status, res = call(server, "/api/import?name=elicit.csv&project=kids", raw=ELICIT_CSV.encode())
    assert status == 200 and res["papers"] == 1
    _, data = call(server, f"/api/runs/{res['run_id']}")
    paper = data["papers"][0]
    assert paper["authors"] == ["Jessica Phillips-Silver", "L. Trainor"] and paper["year"] == 2005
    assert paper["reasons"] == ["Elicit: Closest mechanistic parallel"]
    _, job = call(server, "/api/similar", {"project": "kids", "seed_run": res["run_id"], "embeddings": False})
    assert wait_job(server, job["id"])["status"] in ("done", "failed")  # seed not in the fake OpenAlex
    assert call(server, "/api/import?name=x.txt&project=kids", raw=b"hello")[0] == 400

    out = tmp_path / "my-exports"
    status, exp = call(server, "/api/export", {"project": "kids", "export_dir": str(out)})
    assert status == 200 and (out / "markdown" / "README.md").is_file()
    _, projects = call(server, "/api/projects")
    assert next(p for p in projects if p["name"] == "kids")["export_dir"] == str(out)


def test_upload_and_safety_checks(server: str) -> None:
    status, res = call(server, "/api/upload?name=../../evil.pdf", raw=b"%PDF-1.4 tiny")
    assert status == 200 and "/uploads/" in res["path"] and ".." not in Path(res["path"]).name
    assert call(server, "/api/upload?name=a.pdf", raw=b"not a pdf")[0] == 400
    status, _ = call(server, "/api/similar", {"project": "x", "seeds": ["10.1/x"]},
                     headers={"Origin": "http://evil.example", "Content-Type": "application/json"})
    assert status == 403
    assert call(server, "/api/similar", {"project": "bad/name", "seeds": ["10.1/x"]})[0] == 400
    assert call(server, "/api/similar", {"project": "ok"})[0] == 400  # nothing to search from
    assert call(server, "/api/papers/nope")[0] == 404
