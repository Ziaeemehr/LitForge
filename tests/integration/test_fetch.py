"""Open-access PDF download: arXiv, landing pages with citation_pdf_url, failures, then parse and index."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from discovery_fakes import FakeTransport

from litforge.config import Config, Paths
from litforge.discovery.fetch import fetch_open_access
from litforge.discovery.http import HttpClient
from litforge.ingest import ingest_paths
from litforge.projects import create_project
from litforge.storage.db import now
from litforge.ui import api

LANDING = (b'<html><head><meta name="citation_title" content="B">'
           b"<meta content='/files/b.pdf?x=1&amp;y=2' name='citation_pdf_url'></head></html>")


def _paper(conn: sqlite3.Connection, project_id: str, pid: str, title: str,
           arxiv_id: str | None = None, oa_url: str | None = None) -> None:
    ts = now()
    conn.execute("INSERT INTO papers(id, title, arxiv_id, oa_url, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                 (pid, title, arxiv_id, oa_url, ts, ts))
    conn.execute("INSERT INTO project_papers(project_id, paper_id, source, added_at) VALUES (?,?,?,?)",
                 (project_id, pid, "discovery", ts))


def test_fetch_open_access_downloads_parses_and_indexes(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path
) -> None:
    pid = create_project(conn, paths, "oa")
    _paper(conn, pid, "a" * 32, "Arxiv paper", arxiv_id="2001.00001")
    _paper(conn, pid, "b" * 32, "Publisher paper", oa_url="https://pub.example/landing")
    _paper(conn, pid, "c" * 32, "Paywalled paper", oa_url="https://pub.example/paywall")
    conn.execute("UPDATE papers SET openalex_id='W9', doi='10.1/c' WHERE id=?", ("c" * 32,))
    _paper(conn, pid, "d" * 32, "No source paper")
    _paper(conn, pid, "e" * 32, "Unmarked paper", arxiv_id="2001.00005")
    api.set_marks(conn, "oa", ["a" * 32, "b" * 32, "c" * 32, "d" * 32], "relevant")
    conn.commit()
    transport = FakeTransport([
        ("arxiv.org/pdf/2001.00001", (200, {}, (corpus / "single_column.pdf").read_bytes())),
        ("pub.example/landing", (200, {}, LANDING)),
        ("pub.example/files/b.pdf?x=1&y=2", (200, {}, (corpus / "two_column.pdf").read_bytes())),
        ("pub.example/paywall", (200, {}, b"<html>Buy this article</html>")),
        ("api.openalex.org/works/W9", (200, {}, json.dumps({"locations": [
            {"is_oa": True, "pdf_url": "https://repo.example/c-blocked.pdf", "source": {"type": "repository"}},
            {"is_oa": False, "pdf_url": "https://pub.example/closed.pdf", "source": {"type": "journal"}},
        ]}).encode())),
        ("repo.example/c-blocked.pdf", (403, {}, b"<html>Just a moment...</html>")),
    ])
    http = HttpClient(cache_dir=paths.http_cache, transport=transport, sleep=lambda s: None)

    report = fetch_open_access(conn, paths, config, http, pid)

    assert (report.candidates, report.downloaded, report.parsed, report.no_source) == (4, 2, 2, 1)
    failed = report.failed[0]
    assert len(report.failed) == 1 and "Paywalled" in failed and "no PDF link" in failed
    assert "repo.example: HTTP 403" in failed and "https://doi.org/10.1/c" in failed  # repository tried first
    assert "pub.example/closed.pdf" not in " ".join(transport.calls)  # closed locations are not tried
    assert not any("2001.00005" in u for u in transport.calls)  # only papers marked ✓
    attached = {r["paper_id"] for r in conn.execute("SELECT paper_id FROM artifacts WHERE status='ok'")}
    assert attached == {"a" * 32, "b" * 32}  # attached to the known records, not new papers
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
    assert not list((paths.root / "cache" / "downloads").iterdir())
    cached_hosts = {p.parent.name for p in paths.http_cache.rglob("*.json")}
    assert cached_hosts <= {"api.openalex.org"}  # API lookups may be cached, PDFs never

    again = fetch_open_access(conn, paths, config, http, pid)  # papers with a PDF are skipped
    assert (again.candidates, again.downloaded) == (2, 0)


def test_fetch_same_pdf_as_another_record_is_flagged_once(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path
) -> None:
    other = create_project(conn, paths, "other")
    ingest_paths(conn, paths, [corpus / "single_column.pdf"], other)  # the work, added from a local PDF
    pid = create_project(conn, paths, "oa")
    _paper(conn, pid, "f" * 32, "Same work found by discovery", arxiv_id="2001.00001")
    conn.commit()
    transport = FakeTransport([("arxiv.org/pdf/", (200, {}, (corpus / "single_column.pdf").read_bytes()))])
    http = HttpClient(transport=transport, sleep=lambda s: None)

    report = fetch_open_access(conn, paths, config, http, pid, only="all")
    assert report.downloaded == 0 and "possible duplicate" in report.failed[0]
    members = {r[0] for r in conn.execute("SELECT paper_id FROM project_papers WHERE project_id=?", (pid,))}
    assert members == {"f" * 32}  # the twin record is not pulled into the project
    assert conn.execute("SELECT COUNT(*) FROM duplicate_candidates WHERE paper_id_b=?", ("f" * 32,)).fetchone()[0] == 1
    calls = len(transport.calls)
    assert fetch_open_access(conn, paths, config, http, pid, only="all").candidates == 0
    assert len(transport.calls) == calls  # not downloaded again
