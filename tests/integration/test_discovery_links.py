"""Adding papers from links: article pages, doi.org/arXiv/OpenAlex links and direct PDF links (no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from discovery_fakes import FakeTransport, default_routes
from test_ui_server import call, server, wait_job  # noqa: F401 (server is a fixture)

from litforge.config import Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.links import identifier_from_html, identifier_from_url, run_links
from litforge.discovery.run import Sources
from litforge.projects import create_project

PAGE = b"""<html><head><title>The frontier</title>
<meta name="citation_title" content="The frontier of simulation-based inference">
<meta name="citation_doi" content="10.1073/pnas.1912789117">
</head><body>References: <a href="https://doi.org/10.9999/other">another paper</a></body></html>"""


@pytest.mark.parametrize("url, expected", [
    ("https://doi.org/10.1073/pnas.1912789117", "10.1073/pnas.1912789117"),
    ("http://dx.doi.org/10.1073/PNAS.1912789117", "10.1073/PNAS.1912789117"),
    ("https://arxiv.org/abs/1905.07488v2", "1905.07488"),
    ("https://arxiv.org/pdf/1905.07488.pdf", "1905.07488"),
    ("https://openalex.org/W2984417636", "W2984417636"),
    ("https://link.springer.com/article/10.1007/s10827-020-00745-5", "10.1007/s10827-020-00745-5"),
    ("https://www.pnas.org/doi/full/10.1073/pnas.1912789117?utm=x", "10.1073/pnas.1912789117"),
    ("https://www.nature.com/articles/s41586-020-2649-2", None),  # the page has to be read
])
def test_identifier_from_url(url: str, expected: str | None) -> None:
    assert identifier_from_url(url) == expected


def test_identifier_from_html_reads_the_page_tags_not_its_references() -> None:
    assert identifier_from_html(PAGE.decode()) == "10.1073/pnas.1912789117"
    assert identifier_from_html('<meta name="citation_arxiv_id" content="1905.07488">') == "1905.07488"
    assert identifier_from_html('<meta content="doi:10.5555/x.1" name="dc.identifier">') == "10.5555/x.1"
    assert identifier_from_html("<p>see https://doi.org/10.9999/other</p>") is None


@pytest.fixture()
def pid(conn: sqlite3.Connection, paths: Paths) -> str:
    return create_project(conn, paths, "links")


def test_links_add_their_papers_once_and_report_what_failed(conn: sqlite3.Connection, paths: Paths, pid: str) -> None:
    routes = [("journal.example/articles/frontier", (200, {"Content-Type": "text/html"}, PAGE)),
              ("journal.example/articles/nothing", (200, {"Content-Type": "text/html"}, b"<html>no ids</html>")),
              *default_routes()]
    sources = Sources(HttpClient(transport=FakeTransport(routes), sleep=lambda s: None, min_interval={}))
    report, pdfs = run_links(conn, sources, paths, pid, [
        "https://journal.example/articles/frontier",
        "https://doi.org/10.1073/pnas.1912789117",  # the same paper again
        "https://arxiv.org/abs/1905.07488",
        "https://journal.example/articles/nothing",
        "not a link",
    ])
    titles = sorted(r["title"] for r in conn.execute(
        "SELECT p.title FROM project_papers pp JOIN papers p ON p.id = pp.paper_id WHERE pp.project_id=?", (pid,)))
    assert titles == ["Automatic Posterior Transformation for Likelihood-Free Inference",
                      "The frontier of simulation-based inference"]
    assert report.papers == 2 and report.status == "partial" and pdfs == []
    assert any("articles/nothing" in e and "no DOI" in e for e in report.errors)
    assert any("not a link" in e for e in report.errors)


def test_a_direct_pdf_link_is_downloaded_for_reading(conn: sqlite3.Connection, paths: Paths, pid: str) -> None:
    pdf = b"%PDF-1.4\n%fake\n"
    routes = [("files.example/paper.pdf", (200, {"Content-Type": "application/pdf"}, pdf))]
    sources = Sources(HttpClient(transport=FakeTransport(routes), sleep=lambda s: None, min_interval={}))
    report, pdfs = run_links(conn, sources, paths, pid, ["https://files.example/paper.pdf"])
    assert len(pdfs) == 1 and Path(pdfs[0]).read_bytes() == pdf and Path(pdfs[0]).parent == paths.root / "uploads"
    assert report.errors == []


def test_links_route_adds_the_paper_and_reports_bad_links(server: str) -> None:
    status, job = call(server, "/api/links", {"project": "web", "links": [
        "https://doi.org/10.1073/pnas.1912789117", "nope"]})
    assert status == 200
    job = wait_job(server, job["id"])
    assert job["status"] == "done", job
    assert any("nope" in m and m.startswith("warning") for m in job["messages"])
    assert job["messages"][-1].startswith("done: 1 papers from links")
    _, run = call(server, f"/api/runs/{job['run_id']}")
    assert [p["title"] for p in run["papers"]] == ["The frontier of simulation-based inference"]
    assert call(server, "/api/links", {"project": "web", "links": []})[0] == 400
