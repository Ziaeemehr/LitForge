"""Adding papers from links: article pages, doi.org/arXiv/OpenAlex links and direct PDF links (no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from discovery_fakes import FakeTransport, default_routes

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
    # file and version suffixes are not part of the DOI (review: they made OpenAlex answer 404)
    ("https://link.springer.com/content/pdf/10.1007/s00422-020-00825-5.pdf", "10.1007/s00422-020-00825-5"),
    ("https://www.biorxiv.org/content/10.1101/2020.05.01.072751v2.full.pdf", "10.1101/2020.05.01.072751"),
    ("https://www.biorxiv.org/content/10.1101/2020.05.01.072751v2", "10.1101/2020.05.01.072751"),
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



def test_a_pdf_link_read_twice_is_not_corrupted_by_the_response_cache(conn: sqlite3.Connection, paths: Paths,
                                                                      pid: str) -> None:
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 4  # binary: a text cache would mangle it
    routes = [("files.example/paper.pdf", (200, {"Content-Type": "application/pdf"}, pdf))]
    transport = FakeTransport(routes)
    sources = Sources(HttpClient(cache_dir=paths.http_cache, transport=transport, sleep=lambda s: None,
                                 min_interval={}))
    for _ in range(2):
        report, pdfs = run_links(conn, sources, paths, pid, ["https://files.example/paper.pdf"])
        assert Path(pdfs[0]).read_bytes() == pdf
        assert report.status == "done"  # PDF links alone are a successful run, also in the database
        assert conn.execute("SELECT status FROM discovery_runs WHERE id=?", (report.run_id,)).fetchone()[0] == "done"
    assert len(transport.calls) == 2 and pdfs[0] != run_links(conn, sources, paths, pid,
                                                              ["https://files.example/paper.pdf"])[1][0]


def test_a_doi_not_in_openalex_falls_back_to_reading_the_link(conn: sqlite3.Connection, paths: Paths,
                                                              pid: str) -> None:
    pdf = b"%PDF-1.4\nunlisted\n"
    routes = [("files.example/10.5555/unlisted.1", (200, {"Content-Type": "application/pdf"}, pdf))]
    sources = Sources(HttpClient(transport=FakeTransport(routes), sleep=lambda s: None, min_interval={}))
    report, pdfs = run_links(conn, sources, paths, pid, ["https://files.example/10.5555/unlisted.1"])
    assert len(pdfs) == 1 and report.errors == []
