"""Open-access PDF download: arXiv, landing pages with citation_pdf_url, failures, then parse and index."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pymupdf
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


def test_fetch_chooses_published_copy_before_repository_preprint(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path
) -> None:
    pid = create_project(conn, paths, "ordered-versions")
    _paper(conn, pid, "v" * 32, "Version ordering", oa_url="https://publisher.example/record")
    conn.execute("UPDATE papers SET openalex_id='W-versions' WHERE id=?", ("v" * 32,))
    api.set_marks(conn, "ordered-versions", ["v" * 32], "relevant")
    fixture = (Path(__file__).parents[1] / "fixtures/paper_versions/openalex_locations.json").read_bytes()
    content = (corpus / "single_column.pdf").read_bytes()
    transport = FakeTransport([
        ("api.openalex.org/works/W-versions", (200, {}, fixture)),
        ("publisher.example/article.pdf", (200, {}, content)),
        ("biorxiv.org/content/", (200, {}, content)),
        ("publisher.example/record", (200, {}, content)),
    ])

    report = fetch_open_access(
        conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None), pid,
    )

    assert report.downloaded == 1
    assert transport.calls[1].startswith("https://publisher.example/article.pdf")
    artifact = conn.execute("SELECT version,version_source FROM artifacts WHERE paper_id=?", ("v" * 32,)).fetchone()
    assert tuple(artifact) == ("published", "openalex")


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


def test_fetch_labels_preprint_only_copy(conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path) -> None:
    pid = create_project(conn, paths, "preprint")
    _paper(conn, pid, "g" * 32, "Preprint-only record")
    conn.execute("UPDATE papers SET openalex_id='W-version' WHERE id=?", ("g" * 32,))
    api.set_marks(conn, "preprint", ["g" * 32], "relevant")
    payload = (Path(__file__).parents[1] / "fixtures/paper_versions/openalex_preprint_only.json").read_bytes()
    transport = FakeTransport([
        ("api.openalex.org/works/W-version", (200, {}, payload)),
        ("repository.example/preprint.pdf", (200, {}, (corpus / "single_column.pdf").read_bytes())),
    ])
    http = HttpClient(cache_dir=paths.http_cache, transport=transport, sleep=lambda _: None)

    report = fetch_open_access(conn, paths, config, http, pid)

    artifact = conn.execute("SELECT version,version_source,source_url FROM artifacts "
                            "WHERE paper_id=?", ("g" * 32,)).fetchone()
    assert (artifact["version"], artifact["version_source"]) == ("submitted", "openalex")
    assert artifact["source_url"] == "https://repository.example/preprint.pdf"
    assert report.items[0]["version"] == "submitted"
    assert "preprint downloaded" in report.items[0]["reason"]


def test_preprint_with_its_own_doi_is_not_attached_to_published_record(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path
) -> None:
    pid = create_project(conn, paths, "elife")
    _paper(conn, pid, "h" * 32, "Published eLife record", oa_url="https://biorxiv.org/preprint.pdf")
    conn.execute("UPDATE papers SET doi='10.7554/elife.106194' WHERE id=?", ("h" * 32,))
    api.set_marks(conn, "elife", ["h" * 32], "relevant")
    transport = FakeTransport([
        ("biorxiv.org/preprint.pdf", (200, {}, (corpus / "single_column.pdf").read_bytes())),
    ])
    report = fetch_open_access(
        conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None), pid,
    )

    assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE paper_id=?", ("h" * 32,)).fetchone()[0] == 0
    # The PDF names a DOI that is not in the library: it is skipped, and no record is created for it.
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
    assert "different work" in report.failed[0]
    assert "not in the library" in report.items[0]["reason"]


def _pdf_with_text(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    content = doc.tobytes()
    doc.close()
    return content


def test_fetch_skips_preprint_already_attached_by_earlier_doi_mismatch(
    conn: sqlite3.Connection, paths: Paths, config: Config
) -> None:
    pid = create_project(conn, paths, "elife-repeat")
    _paper(conn, pid, "e" * 32, "Published eLife record", oa_url="https://biorxiv.org/second.pdf")
    _paper(conn, pid, "p" * 32, "bioRxiv preprint record")
    conn.execute("UPDATE papers SET doi='10.7554/elife.106194',openalex_id='W-elife',year=2026 "
                 "WHERE id=?", ("e" * 32,))
    conn.execute("UPDATE papers SET doi='10.1101/2025.01.21.633922',year=2025 WHERE id=?", ("p" * 32,))
    api.set_marks(conn, "elife-repeat", ["e" * 32, "p" * 32], "relevant")
    first_pdf = _pdf_with_text("bioRxiv preprint\nDOI: 10.1101/2025.01.21.633922\nCopy one")
    second_pdf = _pdf_with_text("bioRxiv preprint\nDOI: 10.1101/2025.01.21.633922\nCopy two")
    locations = {"locations": [{
        "is_oa": True, "version": "submittedVersion", "pdf_url": "https://repo.example/first.pdf",
        "source": {"type": "repository"},
    }]}
    transport = FakeTransport([
        ("api.openalex.org/works/W-elife", (200, {}, json.dumps(locations).encode())),
        ("repo.example/first.pdf", (200, {}, first_pdf)),
        ("biorxiv.org/second.pdf", (200, {}, second_pdf)),
    ])

    report = fetch_open_access(conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None),
                               pid, only="all")

    artifacts = conn.execute("SELECT paper_id,sha256 FROM artifacts WHERE status='ok'").fetchall()
    assert [(row["paper_id"], row["sha256"]) for row in artifacts] == [
        ("p" * 32, artifacts[0]["sha256"]),
    ]
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    assert report.downloaded == 1
    assert any(item["reason"] == "already has a PDF" for item in report.items)
    assert not any("biorxiv.org/second.pdf" in call for call in transport.calls)


def test_preprint_host_overrides_accepted_openalex_label(
    conn: sqlite3.Connection, paths: Paths, config: Config
) -> None:
    pid = create_project(conn, paths, "accepted-preprint-host")
    _paper(conn, pid, "a" * 32, "Preprint with accepted label", oa_url="https://biorxiv.org/preprint.pdf")
    conn.execute("UPDATE papers SET openalex_id='W-accepted-host' WHERE id=?", ("a" * 32,))
    api.set_marks(conn, "accepted-preprint-host", ["a" * 32], "relevant")
    payload = {"locations": [{
        "is_oa": True, "version": "acceptedVersion", "pdf_url": "https://biorxiv.org/preprint.pdf",
        "source": {"type": "repository"},
    }]}
    transport = FakeTransport([
        ("api.openalex.org/works/W-accepted-host", (200, {}, json.dumps(payload).encode())),
        ("biorxiv.org/preprint.pdf", (200, {}, _pdf_with_text("A test PDF"))),
    ])

    report = fetch_open_access(conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None), pid)

    artifact = conn.execute("SELECT version,version_source,version_label FROM artifacts "
                            "WHERE paper_id=?", ("a" * 32,)).fetchone()
    assert tuple(artifact) == ("submitted", "host", "bioRxiv preprint (author-updated)")
    assert report.items[0]["version"] == "submitted"


def test_pdf_citing_a_library_paper_on_page_one_stays_with_its_own_record(
    conn: sqlite3.Connection, paths: Paths, config: Config
) -> None:
    pid = create_project(conn, paths, "cites")
    _paper(conn, pid, "o" * 32, "Own record", oa_url="https://pub.example/own.pdf")
    _paper(conn, pid, "k" * 32, "Cited record")
    conn.execute("UPDATE papers SET doi='10.5555/own' WHERE id=?", ("o" * 32,))
    conn.execute("UPDATE papers SET doi='10.5555/cited' WHERE id=?", ("k" * 32,))
    api.set_marks(conn, "cites", ["o" * 32], "relevant")
    transport = FakeTransport([("pub.example/own.pdf", (200, {}, _pdf_with_text(
        "As shown before [1] (doi:10.5555/cited).\nThis article: https://doi.org/10.5555/own")))])

    report = fetch_open_access(conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None), pid)

    assert report.downloaded == 1 and not report.failed
    assert [r[0] for r in conn.execute("SELECT paper_id FROM artifacts")] == ["o" * 32]


def test_damaged_pdf_is_quarantined_without_stopping_the_batch(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path
) -> None:
    pid = create_project(conn, paths, "damaged")
    _paper(conn, pid, "1" * 32, "A damaged copy", oa_url="https://pub.example/broken.pdf")
    _paper(conn, pid, "2" * 32, "A good copy", oa_url="https://pub.example/good.pdf")
    api.set_marks(conn, "damaged", ["1" * 32, "2" * 32], "relevant")
    transport = FakeTransport([
        ("pub.example/broken.pdf", (200, {}, b"%PDF-1.4\n garbage garbage")),
        ("pub.example/good.pdf", (200, {}, (corpus / "single_column.pdf").read_bytes())),
    ])

    report = fetch_open_access(conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None), pid)

    assert report.downloaded == 1
    assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE status='quarantined'").fetchone()[0] == 1
    assert [r[0] for r in conn.execute("SELECT paper_id FROM artifacts WHERE status='ok'")] == ["2" * 32]


def test_doi_mismatch_never_relabels_an_existing_artifact(
    conn: sqlite3.Connection, paths: Paths, config: Config
) -> None:
    import hashlib

    pid = create_project(conn, paths, "existing")
    _paper(conn, pid, "r" * 32, "Published record", oa_url="https://biorxiv.org/copy.pdf")
    _paper(conn, pid, "k" * 32, "Preprint record")
    _paper(conn, pid, "z" * 32, "Record already holding this file")
    conn.execute("UPDATE papers SET doi='10.5555/published' WHERE id=?", ("r" * 32,))
    conn.execute("UPDATE papers SET doi='10.1101/2025.01.21.633922' WHERE id=?", ("k" * 32,))
    api.set_marks(conn, "existing", ["r" * 32], "relevant")
    body = _pdf_with_text("bioRxiv preprint\nDOI: 10.1101/2025.01.21.633922")
    conn.execute("INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,version,version_source,status,"
                 "created_at) VALUES ('kept','zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz',?,'kept.pdf',1,'published','user',"
                 "'ok',?)", (hashlib.sha256(body).hexdigest(), now()))
    transport = FakeTransport([("biorxiv.org/copy.pdf", (200, {}, body))])

    fetch_open_access(conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None), pid)

    kept = conn.execute("SELECT version,version_source,source_url FROM artifacts WHERE id='kept'").fetchone()
    assert tuple(kept) == ("published", "user", None)
