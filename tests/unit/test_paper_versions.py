from __future__ import annotations

import sqlite3
from pathlib import Path

import pymupdf
import pytest
from discovery_fakes import FakeTransport

from litforge.analysis.retrieve import retrieve_for_question
from litforge.config import Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.versions import (
    VersionCandidate,
    infer_version,
    link_explicit_versions,
    link_versions,
    primary_paper_ids,
    rank_candidates,
    set_primary,
)
from litforge.storage.db import now


def test_version_detection_prefers_registry_then_host_then_pdf_text() -> None:
    # A preprint server copy is a preprint even when the registry calls it published or accepted.
    assert infer_version("https://arxiv.org/pdf/1.2", "published", "openalex") == ("submitted", "host")
    assert infer_version("https://journal.example/paper.pdf", "published", "openalex") == ("published", "openalex")
    assert infer_version("https://biorxiv.org/content/1", pdf_text="Final published version") == (
        "submitted", "host",
    )
    assert infer_version("https://publisher.example/paper", pdf_text="Not peer-reviewed") == (
        "submitted", "pdf_text",
    )
    assert infer_version("https://publisher.example/paper") == ("unknown", "unknown")


@pytest.mark.parametrize("text,expected", [
    ("arXiv:2007.09114v2  [cs.LG]  22 Jul 2020", "submitted"),
    ("bioRxiv preprint doi: https://doi.org/10.1101/2025.01.21.633922", "submitted"),
    ("Preprint submitted to Elsevier", "submitted"),
    ("This article is a preprint and has not been certified by peer review.", "submitted"),
    # A published paper citing preprints on its first page stays unknown.
    ("as shown earlier [3]. 3. A. Author, arXiv preprint arXiv:2101.00001 (2021).", "unknown"),
    ("Recent preprints suggest otherwise (see arXiv:2101.00001).", "unknown"),
])
def test_first_page_markers_ignore_citations(text: str, expected: str) -> None:
    assert infer_version("https://publisher.example/paper", pdf_text=text)[0] == expected


def test_rank_candidates_by_version_then_directness() -> None:
    candidates = rank_candidates(
        "2401.12345",
        "https://journal.example/landing",
        openalex_locations=[
            {"is_oa": True, "version": "submittedVersion", "pdf_url": "https://biorxiv.org/file.pdf"},
            {"is_oa": True, "version": "publishedVersion", "landing_page_url": "https://journal.example/page"},
            {"is_oa": True, "version": "publishedVersion", "pdf_url": "https://journal.example/paper.pdf"},
        ],
    )
    assert [(c.url, c.version) for c in candidates] == [
        ("https://journal.example/paper.pdf", "published"),
        ("https://journal.example/page", "published"),
        ("https://arxiv.org/pdf/2401.12345", "submitted"),
        ("https://biorxiv.org/file.pdf", "submitted"),
        ("https://journal.example/landing", "unknown"),
    ]
    assert all(isinstance(c, VersionCandidate) for c in candidates)


def test_unpaywall_version_is_preserved_from_recorded_fixture() -> None:
    from litforge.discovery.fetch import unpaywall_location_records

    payload = (Path(__file__).parents[1] / "fixtures/paper_versions/unpaywall_locations.json").read_bytes()
    transport = FakeTransport([("api.unpaywall.org/v2/10.7554/elife.106194", (200, {}, payload))])
    locations = unpaywall_location_records(
        HttpClient(transport=transport, sleep=lambda _: None), "10.7554/elife.106194", "reader@example.org",
    )
    assert [location["version"] for location in locations] == ["submittedVersion", "publishedVersion"]


def _paper(conn: sqlite3.Connection, paper_id: str, doi: str, openalex: str | None = None) -> None:
    ts = now()
    conn.execute(
        "INSERT INTO papers(id, title, doi, openalex_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (paper_id, f"Paper {paper_id}", doi, openalex, ts, ts),
    )


def test_explicit_linking_and_primary_selection(conn: sqlite3.Connection) -> None:
    _paper(conn, "published", "10.7554/elife.106194")
    _paper(conn, "preprint", "10.1101/2024.01.01.123456", openalex="W123")
    _paper(conn, "unrelated", "10.5555/unrelated")

    links = link_explicit_versions(conn, crossref_relations={
        "10.7554/elife.106194": [{"id-type": "doi", "id": "10.1101/2024.01.01.123456",
                                  "relation": "is-version-of"}],
    })
    assert links == 1
    assert primary_paper_ids(conn) == {"published", "unrelated"}
    set_primary(conn, "preprint")
    assert primary_paper_ids(conn) == {"preprint", "unrelated"}
    members = conn.execute(
        "SELECT paper_id, role, confirmed FROM paper_versions ORDER BY paper_id"
    ).fetchall()
    assert [(r["paper_id"], r["role"], r["confirmed"]) for r in members] == [
        ("preprint", "preprint", 1),
        ("published", "published", 1),
    ]


def test_link_versions_uses_cached_client_and_recorded_crossref_biorxiv_fixtures(
    conn: sqlite3.Connection,
) -> None:
    _paper(conn, "published", "10.7554/elife.106194")
    _paper(conn, "preprint", "10.1101/2024.01.01.123456")
    crossref = (Path(__file__).parents[1] / "fixtures/paper_versions/crossref_relations.json").read_bytes()
    biorxiv = (Path(__file__).parents[1] / "fixtures/paper_versions/biorxiv_published_doi.json").read_bytes()
    transport = FakeTransport([
        ("api.crossref.org/works/10.7554/elife.106194", (200, {}, crossref)),
        ("api.biorxiv.org/details/biorxiv/10.1101/2024.01.01.123456", (200, {}, biorxiv)),
    ])
    http = HttpClient(transport=transport, sleep=lambda _: None)

    assert link_versions(conn, http, "reader@example.org") == 1
    assert any("mailto=reader%40example.org" in call for call in transport.calls)
    assert conn.execute("SELECT COUNT(*) FROM paper_versions WHERE confirmed=1").fetchone()[0] == 2


def test_retrieval_excludes_non_primary_versions(conn: sqlite3.Connection) -> None:
    from lens_fakes import seed

    seed(conn, paper="published", text="Results: Published finding about neural mass oscillations. " * 10)
    seed(conn, paper="preprint", text="Results: Draft finding about neural mass oscillations. " * 10)
    ts = now()
    for paper in ("published", "preprint"):
        conn.execute("INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at) "
                     "VALUES ('group',?,?,?,1,?)", (paper, "published" if paper == "published" else "preprint",
                                                     "test", ts))
    conn.execute("UPDATE paper_versions SET is_primary=1 WHERE paper_id='published'")

    hits = retrieve_for_question(conn, "neural mass oscillations", k=10)
    assert hits and {hit.paper_id for hit in hits} == {"published"}


def test_migration_leaves_existing_artifacts_unknown_and_unlinked(conn: sqlite3.Connection) -> None:
    ts = now()
    conn.execute("INSERT INTO papers(id,created_at,updated_at) VALUES ('legacy',?,?)", (ts, ts))
    conn.execute(
        "INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at)"
        " VALUES ('legacy-pdf','legacy','legacy-hash','legacy.pdf',1,'ok',?)", (ts,),
    )
    artifact = conn.execute(
        "SELECT version,version_source,source_url FROM artifacts WHERE id='legacy-pdf'"
    ).fetchone()
    assert tuple(artifact) == ("unknown", "unknown", None)
    assert conn.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0] == 0


def test_explicit_backfill_detects_preprint_and_marks_cross_record_doi_mismatch(
    conn: sqlite3.Connection, paths: Paths, capsys,
) -> None:
    from litforge.cli import _show_versions
    from litforge.discovery.versions import backfill_unknown_versions
    from litforge.ui.api import library

    _paper(conn, "elife", "10.7554/elife.106194")
    _paper(conn, "preprint", "10.1101/2025.01.21.633922")
    ts = now()
    conn.execute("INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at) "
                 "VALUES ('versions','elife','published','test',1,?)", (ts,))
    conn.execute("INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at) "
                 "VALUES ('versions','preprint','preprint','test',1,?)", (ts,))
    pdf_path = paths.root / "legacy.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "bioRxiv preprint\nDOI: 10.1101/2025.01.21.633922")
    doc.save(pdf_path)
    doc.close()
    conn.execute(
        "INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at) "
        "VALUES ('legacy-mismatch','elife','legacy-mismatch-hash','legacy.pdf',1,'ok',?)", (ts,),
    )

    assert backfill_unknown_versions(conn, paths) == 1

    artifact = conn.execute("SELECT version,version_source,version_label FROM artifacts "
                            "WHERE id='legacy-mismatch'").fetchone()
    assert tuple(artifact) == (
        "submitted", "pdf_text", "this PDF is preprint 10.1101/2025.01.21.633922",
    )
    # The artifact stays on its record (evidence ledger); it is labelled, never silently trusted as published.
    assert primary_paper_ids(conn) == {"elife"}
    rows = {row["id"]: row for row in library(conn, include_versions=True)}
    assert rows["elife"]["pdf_warning"] == artifact["version_label"]
    assert rows["elife"]["primary"] is True
    _show_versions(conn, None)
    assert "WARNING: this PDF is preprint 10.1101/2025.01.21.633922" in capsys.readouterr().out


def test_library_hides_non_primary_versions_unless_requested(conn: sqlite3.Connection) -> None:
    _paper(conn, "published", "10.7554/elife.106194")
    _paper(conn, "preprint", "10.1101/2024.01.01.123456")
    link_explicit_versions(conn, {"10.7554/elife.106194": [
        {"id-type": "doi", "id": "10.1101/2024.01.01.123456", "relation": "has-preprint"},
    ]})

    from litforge.ui.api import library

    assert {p["id"] for p in library(conn)} == {"published"}
    all_rows = library(conn, include_versions=True)
    assert {p["id"] for p in all_rows} == {"published", "preprint"}
    assert all(len(p["versions"]) == 2 for p in all_rows)


def test_similar_titles_are_suggestions_and_rejections_persist(conn: sqlite3.Connection) -> None:
    from litforge.discovery.versions import reject_version_link, suggest_similar_versions

    _paper(conn, "a", "10.1234/a")
    _paper(conn, "b", "10.1234/b")
    conn.execute("UPDATE papers SET title='A long study of neural dynamics and rhythms' WHERE id IN ('a','b')")
    assert suggest_similar_versions(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM paper_versions WHERE confirmed=0").fetchone()[0] == 2
    assert reject_version_link(conn, "a", "b")
    assert suggest_similar_versions(conn) == 0


def test_export_includes_pdf_version_and_group_members(
    conn: sqlite3.Connection, paths: Paths, tmp_path: Path
) -> None:
    from litforge.export.markdown import export_markdown
    from litforge.projects import add_paper, create_project

    project_id = create_project(conn, paths, "versions-export")
    _paper(conn, "published", "10.7554/elife.106194")
    _paper(conn, "preprint", "10.1101/2024.01.01.123456")
    add_paper(conn, project_id, "published", "manual")
    add_paper(conn, project_id, "preprint", "manual")
    link_explicit_versions(conn, {"10.7554/elife.106194": [
        {"id-type": "doi", "id": "10.1101/2024.01.01.123456", "relation": "has-preprint"},
    ]})
    set_primary(conn, "preprint")
    ts = now()
    conn.execute(
        "INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,version,version_source,"
        "source_url,status,created_at)"
        " VALUES ('version-pdf','preprint','version-hash','missing.pdf',1,'submitted','host',"
        "'https://biorxiv.org/content/123v1.full.pdf','ok',?)", (ts,),
    )

    root, count = export_markdown(conn, paths, "versions-export", project_id, out_dir=tmp_path)

    notes = list((root / "knowledge_base").glob("*.md"))
    assert count == 1 and len(notes) == 1
    text = notes[0].read_text()
    assert "version: submitted" in text and "version_source: host" in text
    assert "bioRxiv preprint, v1" in text and "paper_id: preprint" in text
    assert "| Version |" in (root / "README.md").read_text()
