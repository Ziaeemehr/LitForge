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
    assert projects[0]["name"] == "web" and projects[0]["export_dir"].endswith("exports/web")
    _, runs = call(server, "/api/runs?project=web")
    # The seed DOI is shown by its paper's title once the library knows it.
    assert runs[0]["kind"] == "similar" and runs[0]["label"] == "The frontier of simulation-based inference"
    assert runs[0]["project"] == "web"
    _, everywhere = call(server, "/api/runs?limit=5")  # sidebar: all projects
    assert [r["id"] for r in everywhere] == [r["id"] for r in runs][:5]
    _, data = call(server, f"/api/runs/{runs[0]['id']}")
    assert data["run"]["project"] == "web"
    _, lib = call(server, "/api/library")
    assert {p["title"] for p in lib} >= set(titles) and all(p["projects"] == ["web"] for p in lib)

    # "More like my ✓ papers" uses the relevant paper as seed.
    _, job2 = call(server, "/api/similar", {"project": "web", "from_relevant": True, "embeddings": False})
    assert wait_job(server, job2["id"])["status"] == "done"


def test_bulk_marks_and_selective_export(server: str, tmp_path: Path) -> None:
    _, job = call(server, "/api/similar", {"project": "sel", "seeds": ["10.1073/pnas.1912789117"],
                                           "embeddings": False})
    run_id = wait_job(server, job["id"])["run_id"]
    _, data = call(server, f"/api/runs/{run_id}")
    ids = [p["id"] for p in data["papers"]]
    assert len(ids) >= 3
    assert call(server, "/api/marks", {"project": "sel", "paper_ids": ids[:2], "mark": "relevant"})[0] == 200
    assert call(server, "/api/marks", {"project": "sel", "paper_ids": [ids[2]], "mark": "irrelevant"})[0] == 200
    _, data = call(server, f"/api/runs/{run_id}")
    assert data["stats"]["relevant"] == 2 and data["stats"]["irrelevant"] == 1

    out = tmp_path / "sel-exports"
    notes = out / "markdown" / "knowledge_base"
    _, exp = call(server, "/api/export", {"project": "sel", "export_dir": str(out)})
    assert exp["papers"] == len(ids) and len(list(notes.glob("*.md"))) == len(ids)
    (notes / "my-own-note.md").write_text("kept")
    _, exp = call(server, "/api/export", {"project": "sel", "export_dir": str(out), "only": "not-irrelevant"})
    assert exp["papers"] == len(ids) - 1
    _, exp = call(server, "/api/export", {"project": "sel", "export_dir": str(out), "only": "relevant"})
    written = {md.stem for md in notes.glob("*.md")}
    assert exp["papers"] == 2 and len(written) == 3 and "my-own-note" in written  # stale notes removed, own kept
    _, exp = call(server, "/api/export", {"project": "sel", "export_dir": str(out), "paper_ids": [ids[2]]})
    assert exp["papers"] == 1
    (notes / "meeting-20240115.md").write_text("mine")  # looks like a generated name, but is not one
    _, exp = call(server, "/api/export", {"project": "sel", "export_dir": str(out), "paper_ids": []})
    assert exp["papers"] == 0 and len(list(notes.glob("*.md"))) == 3  # empty selection leaves the folder alone
    _, exp = call(server, "/api/export", {"project": "sel", "export_dir": str(out)})
    assert (notes / "meeting-20240115.md").is_file() and (notes / "my-own-note.md").is_file()
    assert call(server, "/api/export", {"project": "sel", "only": "bogus"})[0] == 400

    # Export with PDFs: a background job that downloads (nothing reachable here), then exports.
    _, job = call(server, "/api/export", {"project": "sel", "export_dir": str(out), "only": "relevant",
                                          "fetch": True, "run_id": run_id})
    job = wait_job(server, job["id"])
    assert job["status"] == "done" and job["run_id"] == run_id
    assert job["messages"][-1] == f"done: exported 2 papers to {out / 'markdown'}"

    assert call(server, "/api/marks", {"project": "sel", "paper_ids": ids, "mark": None})[0] == 200
    _, data = call(server, f"/api/runs/{run_id}")
    assert data["stats"]["relevant"] == 0 and data["stats"]["irrelevant"] == 0


def test_add_my_pdfs_from_folder_and_uploads(server: str, corpus: Path, tmp_path: Path) -> None:
    status, up = call(server, "/api/upload?name=two_column.pdf", raw=(corpus / "two_column.pdf").read_bytes())
    assert status == 200
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "a.pdf").write_bytes((corpus / "single_column.pdf").read_bytes())
    (folder / "broken.pdf").write_bytes(b"not a pdf")

    chosen = tmp_path / "vault"  # a new project's export folder, chosen before its first run
    _, job = call(server, "/api/add", {"project": "own", "folder": str(folder), "files": [up["path"]],
                                       "export_dir": str(chosen)})
    job = wait_job(server, job["id"])
    assert job["status"] == "done", job
    assert job["messages"][-1].startswith("done: 3 PDFs, 2 new")
    assert not Path(up["path"]).exists()  # upload removed once it is in the library
    _, data = call(server, f"/api/runs/{job['run_id']}")
    assert data["run"]["kind"] == "pdfs" and data["run"]["label"] == "mine" and len(data["papers"]) == 2
    assert {r for p in data["papers"] for r in p["reasons"]} == {"your PDF: a.pdf", "your PDF: two_column.pdf"}
    assert all(p["has_pdf"] for p in data["papers"])
    paper = data["papers"][0]
    _, detail = call(server, f"/api/papers/{paper['id']}")
    status, pdf = call(server, detail["pdf_url"])
    assert status == 200 and pdf.startswith(b"%PDF")
    _, exp = call(server, "/api/export", {"project": "own"})
    assert exp["path"] == str(chosen / "markdown")
    pdfs = sorted((Path(exp["path"]) / "pdfs").glob("*.pdf"))
    assert len(pdfs) == 2 and all(p.read_bytes().startswith(b"%PDF") for p in pdfs)
    note = next((Path(exp["path"]) / "knowledge_base").glob("*.md")).read_text()
    assert "pdf: ../pdfs/" in note and "- [PDF](../pdfs/" in note

    _, job = call(server, "/api/add", {"project": "own", "folder": str(folder)})  # re-run: nothing new
    assert wait_job(server, job["id"])["messages"][-1].startswith("done: 2 PDFs, 0 new, 1 already in the library")
    assert call(server, "/api/add", {"project": "own", "folder": str(tmp_path / "missing")})[0] == 400
    assert call(server, "/api/add", {"project": "own", "files": [str(folder / "a.pdf")]})[0] == 400
    assert call(server, "/api/add", {"project": "own"})[0] == 400


def test_delete_run_undoes_its_additions(server: str) -> None:
    _, job = call(server, "/api/similar", {"project": "undo", "seeds": ["10.1073/pnas.1912789117"],
                                           "embeddings": False})
    first = wait_job(server, job["id"])["run_id"]
    _, job = call(server, "/api/similar", {"project": "undo", "seeds": ["10.1073/pnas.1912789117"],
                                           "embeddings": False})
    second = wait_job(server, job["id"])["run_id"]
    _, data = call(server, f"/api/runs/{first}")
    ids = [p["id"] for p in data["papers"]]
    call(server, "/api/marks", {"project": "undo", "paper_ids": ids[:1], "mark": "relevant"})

    # Every paper of the first run was found again by the second one: nothing leaves the project yet.
    status, res = call(server, "/api/delete-run", {"run_id": first})
    assert status == 200 and res["removed"] == 0
    _, runs = call(server, "/api/runs?project=undo")
    assert [r["id"] for r in runs] == [second]
    # With no run left, its papers leave the project, except the one marked ✓.
    _, res = call(server, "/api/delete-run", {"run_id": second})
    assert res == {"removed": len(ids) - 1, "kept": 1}
    _, projects = call(server, "/api/projects")
    assert next(p for p in projects if p["name"] == "undo")["papers"] == 1
    assert call(server, "/api/delete-run", {"run_id": second})[0] == 400


def test_delete_project_keeps_library_and_exports(server: str, tmp_path: Path) -> None:
    _, job = call(server, "/api/similar", {"project": "gone", "seeds": ["10.1073/pnas.1912789117"],
                                           "embeddings": False})
    run_id = wait_job(server, job["id"])["run_id"]
    _, exp = call(server, "/api/export", {"project": "gone"})
    status, res = call(server, "/api/delete-project", {"project": "gone"})
    assert status == 200 and res["papers"] > 0 and res["kept_exports"].endswith("exports/gone")
    assert (Path(exp["path"]) / "README.md").is_file()  # the user's exports survive
    _, projects = call(server, "/api/projects")
    assert "gone" not in [p["name"] for p in projects]
    assert call(server, f"/api/runs/{run_id}")[0] in (400, 404)
    assert call(server, "/api/delete-project", {"project": "gone"})[0] == 400

    # With delete_exports, only the markdown folder LitForge wrote goes; the rest of a custom folder stays.
    vault = tmp_path / "vault"
    (vault / "my-notes").mkdir(parents=True)
    _, job = call(server, "/api/similar", {"project": "gone2", "seeds": ["10.1073/pnas.1912789117"],
                                           "embeddings": False, "export_dir": str(vault)})
    wait_job(server, job["id"])
    call(server, "/api/export", {"project": "gone2"})
    assert (vault / "markdown" / "README.md").is_file()
    _, res = call(server, "/api/delete-project", {"project": "gone2", "delete_exports": True})
    assert res["kept_exports"] is None and not (vault / "markdown").exists() and (vault / "my-notes").is_dir()


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
    assert call(server, "/api/export-dir", {"project": "kids", "export_dir": ""})[0] == 200
    _, projects = call(server, "/api/projects")
    assert next(p for p in projects if p["name"] == "kids")["export_dir"].endswith("exports/kids")


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
