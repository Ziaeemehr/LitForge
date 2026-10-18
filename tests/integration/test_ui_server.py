"""Web UI server: real HTTP on a free local port, discovery through the fake transport."""

from __future__ import annotations

import json
import threading
import time
import types
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
from litforge.parsing.formulas import FakeFormulas, Formula
from litforge.parsing.vision import FakeVision
from litforge.ui.server import App, Job, make_handler, stop_when_idle

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
    vision = FakeVision(lambda png, text: text + "\n\n$$\nx = \\frac{a}{b} \\tag{1}\n$$")
    app = App(Paths(tmp_path / "data"), config,
              make_http=lambda: HttpClient(transport=transport, sleep=lambda s: None, min_interval={}),
              make_vision=lambda: vision,
              make_formulas=lambda: FakeFormulas(lambda png: [Formula(box=(0, 0, 4000, 4000), kind="isolated",
                                                                      latex=r"\frac{c}{d}")]))
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
    assert b'id="screen-panel"' in html and b'value="screening"' in html
    _, js = call(server, "/static/app.js")
    assert b"/api/screen" in js and b"/api/criteria" in js


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

    # Papers still without a PDF, for downloading by hand; the scope follows the marks like export does.
    _, missing = call(server, "/api/missing-pdfs?project=sel")
    assert {p["id"] for p in missing} == set(ids) and all("doi" in p and "oa_url" in p for p in missing)
    _, missing = call(server, "/api/missing-pdfs?project=sel&only=relevant")
    assert {p["id"] for p in missing} == set(ids[:2]) and {p["mark"] for p in missing} == {"relevant"}
    assert call(server, "/api/missing-pdfs?project=sel&only=bogus")[0] == 400

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
    report = job["report"]  # what happened to each file, and where they are kept
    by_file = {i["file"]: i for i in report["items"]}
    assert set(by_file) == {"a.pdf", "broken.pdf", "two_column.pdf"}
    assert by_file["a.pdf"]["outcome"] == "new_paper" and by_file["a.pdf"]["title"] and by_file["a.pdf"]["full_text"]
    assert by_file["broken.pdf"]["outcome"] == "failed" and by_file["broken.pdf"]["reason"]
    assert report["library"].endswith("library/pdfs") and report["export_dir"] == str(chosen.resolve())
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
    pdfs = sorted((chosen / "pdfs").glob("*.pdf"))
    assert len(pdfs) == 2 and all(p.read_bytes().startswith(b"%PDF") for p in pdfs)
    note = next((Path(exp["path"]) / "knowledge_base").glob("*.md")).read_text()
    assert "pdf: ../../pdfs/" in note and "- [PDF](../../pdfs/" in note

    _, job = call(server, "/api/add", {"project": "own", "folder": str(folder)})  # re-run: nothing new
    assert wait_job(server, job["id"])["messages"][-1].startswith("done: 2 PDFs, 0 new, 1 already in the library")
    assert call(server, "/api/add", {"project": "own", "folder": str(tmp_path / "missing")})[0] == 400
    assert call(server, "/api/add", {"project": "own", "files": [str(folder / "a.pdf")]})[0] == 400
    assert call(server, "/api/add", {"project": "own"})[0] == 400


def test_add_my_pdfs_and_export(server: str, corpus: Path, tmp_path: Path) -> None:
    out = tmp_path / "notes"
    folder = tmp_path / "first"
    folder.mkdir()
    (folder / "a.pdf").write_bytes((corpus / "single_column.pdf").read_bytes())
    _, job = call(server, "/api/add", {"project": "auto", "folder": str(folder), "export_dir": str(out),
                                       "export": {"only": "all"}})
    job = wait_job(server, job["id"])
    assert job["status"] == "done", job
    assert job["report"]["exported"] == {"papers": 1, "path": str(out / "markdown")}
    assert "exported 1 papers to" in job["messages"][-1]

    # A second batch exports the whole selection again, so the first paper's note stays.
    second = tmp_path / "second"
    second.mkdir()
    (second / "b.pdf").write_bytes((corpus / "two_column.pdf").read_bytes())
    _, job = call(server, "/api/add", {"project": "auto", "folder": str(second), "export": {"only": "all"}})
    job = wait_job(server, job["id"])
    assert job["report"]["exported"]["papers"] == 2
    assert len(list((out / "markdown" / "knowledge_base").glob("*.md"))) == 2
    assert len(list((out / "pdfs").glob("*.pdf"))) == 2

    # An explicit selection still includes the papers the PDFs went to.
    _, job = call(server, "/api/add", {"project": "auto", "folder": str(second), "export": {"paper_ids": []}})
    assert wait_job(server, job["id"])["report"]["exported"]["papers"] == 1
    assert call(server, "/api/add", {"project": "auto", "folder": str(second), "export": {"only": "x"}})[0] == 400
    assert call(server, "/api/add", {"project": "auto", "folder": str(second), "export": True})[0] == 400

    # When the notes cannot be written the PDFs are still added, and the report says why.
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the export folder should be")
    _, job = call(server, "/api/add", {"project": "blocked", "folder": str(folder), "export_dir": str(blocked),
                                       "export": {"only": "all"}})
    job = wait_job(server, job["id"])
    assert job["status"] == "done" and job["report"]["export_error"] and "exported" not in job["report"]
    assert any(m.startswith("warning: export failed") for m in job["messages"])


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
    (vault / "pdfs").mkdir()
    (vault / "pdfs" / "mine.pdf").write_bytes(b"%PDF-1.4")  # the user's own file in a same-named folder
    _, job = call(server, "/api/similar", {"project": "gone2", "seeds": ["10.1073/pnas.1912789117"],
                                           "embeddings": False, "export_dir": str(vault)})
    wait_job(server, job["id"])
    call(server, "/api/export", {"project": "gone2"})
    assert (vault / "markdown" / "README.md").is_file()
    _, res = call(server, "/api/delete-project", {"project": "gone2", "delete_exports": True})
    assert res["kept_exports"] is None and not (vault / "markdown").exists() and (vault / "my-notes").is_dir()
    assert [p.name for p in (vault / "pdfs").iterdir()] == ["mine.pdf"]  # only the PDFs LitForge copied go


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


@pytest.fixture()
def screen_server(tmp_path: Path) -> Iterator[str]:
    from litforge.screening.llm import FakeLlm

    transport = FakeTransport(default_routes())
    config = load_config()
    config.similar.embedding_model = None

    def answer(messages: list[dict[str, str]]) -> dict[str, Any]:
        return {"relevance": 4, "inclusion": [{"status": "met", "evidence": ""}], "exclusion": [], "reason": "fits"}

    app = App(Paths(tmp_path / "data"), config,
              make_http=lambda: HttpClient(transport=transport, sleep=lambda s: None, min_interval={}),
              make_llm=lambda model: FakeLlm(answer))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_criteria_screen_job_and_screening_fields(screen_server: str) -> None:
    s = screen_server
    _, job = call(s, "/api/similar", {"project": "scr", "seeds": ["10.1073/pnas.1912789117"], "embeddings": False})
    run_id = wait_job(s, job["id"])["run_id"]
    status, err = call(s, "/api/screen", {"run_id": run_id, "top": 3})
    assert status == 400 and "criteria" in err["error"]

    status, crit = call(s, "/api/criteria", {"project": "scr", "question": "q", "include": "a\n\nb", "exclude": ""})
    assert status == 200 and crit["include"] == ["a", "b"] and crit["model"]
    assert call(s, "/api/criteria?project=scr")[1]["question"] == "q"

    assert call(s, "/api/screen", {"run_id": run_id, "top": 0})[0] == 400
    status, job = call(s, "/api/screen", {"run_id": run_id, "top": 3})
    assert status == 200
    job = wait_job(s, job["id"])
    assert job["status"] == "done" and job["run_id"] == run_id, job

    _, data = call(s, f"/api/runs/{run_id}")
    screened = [p for p in data["papers"] if p["screening"]]
    assert len(screened) == min(3, len(data["papers"]))
    assert screened[0]["screening"]["decision"] == "include"  # "b" padded to unclear, nothing not_met, relevance 4
    assert data["stats"]["screened"] == len(screened) == data["stats"]["screen_include"]

    _, detail = call(s, f"/api/papers/{screened[0]['id']}?project=scr")
    assert detail["screening"]["inclusion"][0]["criterion"] == "a" and detail["screening"]["model"] == "fake"

    call(s, "/api/criteria", {"project": "scr", "question": "changed", "include": "", "exclude": ""})
    _, data = call(s, f"/api/runs/{run_id}")
    assert all(p["screening"]["stale"] for p in data["papers"] if p["screening"])


def test_contact_email_setting_and_fetch_job(server: str, tmp_path: Path) -> None:
    assert call(server, "/api/settings")[1]["mailto"] is None
    assert call(server, "/api/settings", {"mailto": "not an email"})[0] == 400
    status, res = call(server, "/api/settings", {"mailto": " me@uni.edu "})
    assert status == 200 and res["mailto"] == "me@uni.edu"
    assert call(server, "/api/settings")[1]["mailto"] == "me@uni.edu"
    assert json.loads((tmp_path / "data" / "settings.json").read_text())["mailto"] == "me@uni.edu"

    # "Find PDFs again" from the missing-PDFs window: download only, for the listed papers.
    _, job = call(server, "/api/similar", {"project": "dl", "seeds": ["10.1073/pnas.1912789117"],
                                           "embeddings": False})
    wait_job(server, job["id"])
    _, missing = call(server, "/api/missing-pdfs?project=dl")
    _, job = call(server, "/api/fetch", {"project": "dl", "paper_ids": [p["id"] for p in missing]})
    job = wait_job(server, job["id"])
    assert job["status"] == "done" and "missing PDFs downloaded" in job["messages"][-1]
    report = job["report"]  # one line per paper, so the page can say what happened to each
    assert report["downloaded"] == 0 and len(report["items"]) == len(missing)
    assert {i["outcome"] for i in report["items"]} <= {"failed", "no_source"}
    assert all(i["reason"] for i in report["items"])
    assert report["library"].endswith("library/pdfs") and report["export_dir"].endswith("dl")

    assert call(server, "/api/settings", {"mailto": ""})[1]["mailto"] is None  # cleared


def test_export_with_better_math_runs_the_vision_model(server: str, corpus: Path, tmp_path: Path) -> None:
    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "a.pdf").write_bytes((corpus / "single_column.pdf").read_bytes())
    _, job = call(server, "/api/add", {"project": "math", "folder": str(folder)})
    wait_job(server, job["id"])
    _, job = call(server, "/api/export", {"project": "math", "vision": True})
    job = wait_job(server, job["id"])
    assert job["status"] == "done", job
    vis = job["report"]["vision"]
    assert vis["papers"] == 1 and vis["pages_improved"] >= 1 and vis["items"][0]["title"]
    assert any("pages rewritten" in m for m in job["messages"]) and job["messages"][-1].startswith("done: exported 1")
    note = next(Path(job["report"]["export_dir"], "markdown", "knowledge_base").glob("*.md")).read_text()
    assert "\\frac{a}{b}" in note


def test_export_with_math_medium_uses_the_formula_engine(server: str, corpus: Path, tmp_path: Path) -> None:
    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "a.pdf").write_bytes((corpus / "single_column.pdf").read_bytes())
    _, job = call(server, "/api/add", {"project": "med", "folder": str(folder)})
    wait_job(server, job["id"])
    assert call(server, "/api/export", {"project": "med", "math": "bogus"})[0] == 400
    _, job = call(server, "/api/export", {"project": "med", "math": "medium"})
    job = wait_job(server, job["id"])
    assert job["status"] == "done", job
    assert job["report"]["vision"]["level"] == "medium" and job["report"]["vision"]["pages_improved"] >= 1
    note = next(Path(job["report"]["export_dir"], "markdown", "knowledge_base").glob("*.md")).read_text()
    assert "\\frac{c}{d}" in note
    _, info = call(server, "/api/info")
    assert set(info["math"]) == {"medium", "high"}


def test_startup_creates_the_library_and_marks_unfinished_runs_interrupted(tmp_path: Path) -> None:
    from litforge.projects import create_project
    from litforge.storage.db import open_db

    paths = Paths(tmp_path / "data")
    App(paths, load_config())  # a fresh data folder: folders and database are created
    assert paths.db.is_file() and paths.pdfs.is_dir()
    conn = open_db(paths.db)
    pid = create_project(conn, paths, "p")
    conn.execute("INSERT INTO discovery_runs(id, project_id, kind, params_json, status, started_at)"
                 " VALUES ('r1', ?, 'search', '{}', 'running', '2026-01-01')", (pid,))
    conn.commit()
    conn.close()
    App(paths, load_config())  # a restart: the old server's job will never finish
    conn = open_db(paths.db)
    assert conn.execute("SELECT status FROM discovery_runs WHERE id='r1'").fetchone()[0] == "interrupted"
    conn.close()


def test_quit_stops_the_server_and_asks_first_when_a_task_runs(tmp_path: Path) -> None:
    app = App(Paths(tmp_path / "data"), load_config())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert call(base, "/api/quit", {})[0] == 400  # not started by serve(): nothing to stop
        app.stop = httpd.shutdown
        app.jobs["j"] = Job(id="j", kind="search", project="p")
        assert call(base, "/api/quit", {}) == (200, {"stopped": False, "running": 1})
        assert call(base, "/api/quit", {"force": True}) == (200, {"stopped": True})
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        httpd.server_close()


def test_a_page_check_in_is_accepted_and_refreshes_the_idle_clock(server: str) -> None:
    assert call(server, "/api/heartbeat", {}) == (200, {"ok": True})


class _Httpd:  # stands in for the server: records that it was asked to stop
    def __init__(self) -> None:
        self.stopped = threading.Event()

    def shutdown(self) -> None:
        self.stopped.set()


def test_an_idle_server_stops_itself_but_a_running_job_keeps_it_up() -> None:
    app = types.SimpleNamespace(jobs={}, last_seen=time.monotonic() - 10)
    httpd = _Httpd()
    stop_when_idle(app, httpd, idle_s=0.2, check_s=0.01)  # type: ignore[arg-type]
    assert httpd.stopped.wait(2)

    app = types.SimpleNamespace(jobs={"j": Job(id="j", kind="search", project="p")}, last_seen=time.monotonic() - 10)
    httpd = _Httpd()
    stop_when_idle(app, httpd, idle_s=0.2, check_s=0.01)  # type: ignore[arg-type]
    assert not httpd.stopped.wait(0.5)


class FakeSetup:
    def __init__(self) -> None:
        self.ran = 0

    def status(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(as_dict=lambda: {"ready": self.ran > 0, "model": "m"})

    def run(self, progress: Any) -> None:
        self.ran += 1
        progress("done: m is ready for better math (high)")


def test_math_setup_status_and_job(tmp_path: Path) -> None:
    setup = FakeSetup()
    app = App(Paths(tmp_path / "data"), load_config(), vision_setup=setup)  # type: ignore[arg-type]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert call(base, "/api/math-setup")[1]["ready"] is False
        _, job = call(base, "/api/math-setup", {"project": "p"})
        job = wait_job(base, job["id"])
        assert job["status"] == "done" and job["kind"] == "setup" and job["messages"][-1].startswith("done:")
        assert call(base, "/api/math-setup")[1]["ready"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_optional_tools_status_and_pix2text_install_origin_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import litforge.ui.server as server_module

    app = App(Paths(tmp_path / "data"), load_config())
    monkeypatch.setattr(server_module, "mineru_status", lambda *args: {"installed": False, "version": None})
    monkeypatch.setattr(server_module, "formulas_available", lambda: False)
    pix_status = {
        "offerable": True, "installed": False, "command": "uv sync --extra formulas --inexact",
        "extra": "formulas", "size": "~2 GB", "version": None, "gpu": False,
    }
    monkeypatch.setattr(server_module, "pix2text_install_status", lambda: pix_status)
    monkeypatch.setattr(app, "math_setup_status", lambda: {"ready": False, "model": "vision-model"})
    monkeypatch.setattr(server_module.App, "start_job", lambda self, kind, project, work:
                        Job(id="pix-job", kind=kind, project=project))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, tools = call(base, "/api/tools")
        assert status == 200
        assert tools["pix2text"]["offerable"] is True
        assert tools["vision"]["model"] == "vision-model"
        assert call(base, "/api/tools/pix2text/install", {}, headers={"Origin": "https://evil.example"})[0] == 403
        assert call(base, "/api/tools/pix2text/install", {})[0] == 400
        assert call(base, "/api/tools/pix2text/install", {"confirmed": True}) == (200, {
            "id": "pix-job", "kind": "pix2text-install", "project": "", "status": "running",
            "messages": [], "run_id": None, "error": None, "report": None,
        })
        pix_status["offerable"] = False
        refused, error = call(base, "/api/tools/pix2text/install", {"confirmed": True})
        assert refused == 400 and "uv sync --extra formulas" in error["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_export_with_math_high_and_no_ollama_says_why(tmp_path: Path) -> None:
    config = load_config()
    probe = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(Paths(tmp_path / "x"), config)))
    config.screening.ollama_url = f"http://127.0.0.1:{probe.server_address[1]}"
    probe.server_close()  # a port nobody listens on
    app = App(Paths(tmp_path / "data"), config)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, res = call(base, "/api/export", {"project": "p", "math": "high"})
        assert status == 400 and "not reachable" in res["error"], res
    finally:
        httpd.shutdown()
        httpd.server_close()
