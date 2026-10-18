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
    assert infer_version("https://publisher.example/paper", pdf_text="This manuscript has not been peer reviewed") == (
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

    assert backfill_unknown_versions(conn, paths) == (1, 0)

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
    from litforge.discovery.versions import versions_for_paper

    assert all(len(versions_for_paper(conn, p["id"])) == 2 for p in all_rows)


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


# ---- review fixes -------------------------------------------------------------------------------------------

def _group(conn: sqlite3.Connection, group: str, members: dict[str, str], primary: str | None = None) -> None:
    for paper, role in members.items():
        conn.execute("INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,is_primary,created_at) "
                     "VALUES (?,?,?,'test',1,?,?)", (group, paper, role, int(paper == primary), now()))


def _two_projects(conn: sqlite3.Connection) -> None:
    """`pre` is in project `proj`, `pub` in project `B`: each project's primary is its own member."""
    from lens_fakes import seed

    seed(conn, paper="pre", text="Results: draft finding oscillations. " * 5)
    seed(conn, paper="pub", text="Results: published finding oscillations. " * 5)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('B','b','now')")
    conn.execute("DELETE FROM project_papers WHERE paper_id='pub'")
    conn.execute("INSERT INTO project_papers(project_id,paper_id,source,added_at) VALUES ('B','pub','manual','now')")
    _group(conn, "g", {"pre": "preprint", "pub": "published"})


def test_paper_scoped_search_keeps_the_callers_choice_across_projects(conn: sqlite3.Connection) -> None:
    from litforge.indexing.fts import search

    _two_projects(conn)
    assert primary_paper_ids(conn, "proj") == {"pre"}
    assert primary_paper_ids(conn) == {"pub"}
    # matrix / claims / full-text screening search one paper without a project.
    assert [h.paper_id for h in search(conn, "oscillations", paper_ids=["pre"])] == ["pre"]
    assert {h.paper_id for h in search(conn, "oscillations", "proj")} == {"pre"}
    assert {h.paper_id for h in search(conn, "oscillations")} == {"pub"}


def test_search_binds_no_library_ids_and_excludes_only_non_primary(conn: sqlite3.Connection) -> None:
    from lens_fakes import seed

    from litforge.indexing.fts import search

    for i in range(50):
        seed(conn, paper=f"p{i}", text="Results: shared oscillations. " * 3)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    assert len(search(conn, "oscillations", limit=100, max_per_paper=1)) == 50
    query = next(s for s in statements if "chunks_fts MATCH" in s)
    assert "'p7'" not in query and "NOT IN" not in query
    _group(conn, "g", {"p1": "published", "p2": "preprint"})
    statements.clear()
    assert len(search(conn, "oscillations", limit=100, max_per_paper=1)) == 49
    query = next(s for s in statements if "chunks_fts MATCH" in s)
    assert "NOT IN ('p2')" in query and "'p7'" not in query
    conn.set_trace_callback(None)


def test_explicitly_selected_non_primary_papers_are_included(conn: sqlite3.Connection) -> None:
    from lens_fakes import gateway

    from litforge.analysis import matrix as matrices
    from litforge.analysis.claims import paper_runs
    from litforge.analysis.matrix import MatrixColumn, create_matrix
    from litforge.screening import fulltext
    from litforge.screening.criteria import Criteria, save_criteria

    _two_projects(conn)
    conn.execute("INSERT INTO project_papers(project_id,paper_id,source,added_at) VALUES ('proj','pub','manual','now')")
    assert primary_paper_ids(conn, "proj") == {"pub"}
    assert set(paper_runs(conn, "proj")) == {"pub"}
    assert set(paper_runs(conn, "proj", ["pre"])) == {"pre"}
    assert {h.paper_id for h in retrieve_for_question(conn, "oscillations", "proj", paper_ids=["pre"])} == {"pre"}
    gw, _ = gateway(conn)
    matrix = create_matrix(conn, "proj", "M", "oscillations",
                           [MatrixColumn(key="gain", label="Gain", instruction="gain", type="text")])
    assert matrices.plan_calls(conn, gw, matrix.id, ["pre"])  # no "outside the matrix project" error
    save_criteria(conn, "proj", Criteria(question="oscillations"))
    needs_pdf, work, _ = fulltext._work(conn, gw, "proj", None, None, True, ["pre"])
    assert [w[0] for w in work] == ["pre"] and needs_pdf == []


def test_suggestions_scale_to_a_large_library(conn: sqlite3.Connection) -> None:
    import random
    import time

    from litforge.discovery.versions import suggest_similar_versions

    rng = random.Random(7)
    vocabulary = [f"word{i:04d}x" for i in range(3000)]
    rows = []
    for i in range(5000):
        title = " ".join(rng.choice(vocabulary) for _ in range(9))
        rows.append((f"s{i}", title, '["Ada Lovelace"]', 2020))
    rows += [("dup1", rows[10][1] + "s", '["Ada Lovelace"]', 2021),
             ("dup2", rows[20][1].upper(), '["Ada Lovelace"]', 2020)]
    conn.executemany("INSERT INTO papers(id,title,authors_json,year,created_at,updated_at) "
                     "VALUES (?,?,?,?,'now','now')", rows)
    started = time.perf_counter()
    assert suggest_similar_versions(conn) == 2
    assert time.perf_counter() - started < 5
    assert suggest_similar_versions(conn) == 0
    conn.execute("INSERT INTO papers(id,title,authors_json,year,created_at,updated_at) "
                 "VALUES ('dup3',?,'[\"Ada Lovelace\"]',2020,'now','now')", (rows[30][1],))
    assert suggest_similar_versions(conn, paper_ids=["s1", "s2"]) == 0  # scoped: dup3 is out of scope
    assert suggest_similar_versions(conn, paper_ids=["dup3"]) == 1


@pytest.mark.parametrize("doi,arxiv_id,pdf_version,expected", [
    ("10.1103/physrevlett.1.1", "2101.00001", None, "published"),   # a journal record with an arXiv id
    (None, "2101.00001", None, "preprint"),
    ("10.48550/arxiv.2101.00001", None, None, "preprint"),
    ("10.1101/2024.01.01.123456", None, None, "preprint"),
    ("10.1101/123456", None, None, "preprint"),
    ("10.1101/gr.123456.111", None, None, "published"),             # Genome Research
    ("10.1101/gad.1234567", None, None, "published"),
    ("10.1101/cshperspect.a012345", None, None, "published"),
    ("10.1101/lm.012345.110", None, None, "published"),
    ("10.31234/osf.io/abcde", None, None, "preprint"),              # PsyArXiv
    ("10.31235/osf.io/abcde", None, None, "preprint"),              # SocArXiv
    ("10.26434/chemrxiv-2021-abc", None, None, "preprint"),
    ("10.20944/preprints202101.0001.v1", None, None, "preprint"),
    ("10.7554/elife.106194", None, "submitted", "published"),       # the record, not its preprint PDF
    (None, None, "accepted", "accepted"),
    (None, None, None, "unknown"),
])
def test_record_role_comes_from_the_doi_first(
    conn: sqlite3.Connection, doi: str | None, arxiv_id: str | None, pdf_version: str | None, expected: str,
) -> None:
    from litforge.discovery.versions import _role_for_paper

    conn.execute("INSERT INTO papers(id,title,doi,arxiv_id,created_at,updated_at) VALUES ('r','R',?,?,'now','now')",
                 (doi, arxiv_id))
    if pdf_version:
        conn.execute("INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,version,status,created_at) "
                     "VALUES ('ra','r','rsha','r.pdf',1,?,'ok','now')", (pdf_version,))
    assert _role_for_paper(conn, "r") == expected


def test_doi_revision_suffix_only_for_version_registrants(conn: sqlite3.Connection) -> None:
    _paper(conn, "book", "10.1017/CBO9780511810817")
    _paper(conn, "chapter", "10.1017/CBO9780511810817.003")
    assert link_explicit_versions(conn) == 0
    _paper(conn, "elife", "10.7554/elife.106194")
    _paper(conn, "elife1", "10.7554/elife.106194.1")
    _paper(conn, "f1000", "10.12688/f1000research.12345.1")
    _paper(conn, "f1000-2", "10.12688/f1000research.12345.2")
    _paper(conn, "f1000-base", "10.12688/f1000research.12345")
    assert link_explicit_versions(conn) == 3
    groups = {r["paper_id"]: r["group_id"] for r in conn.execute("SELECT paper_id,group_id FROM paper_versions")}
    assert set(groups) == {"elife", "elife1", "f1000", "f1000-2", "f1000-base"}
    assert groups["elife"] == groups["elife1"] and groups["f1000"] == groups["f1000-2"] == groups["f1000-base"]


@pytest.mark.parametrize("text,expected", [
    ("This manuscript has not been peer reviewed.", "submitted"),
    ("This preprint has not yet been peer-reviewed.", "submitted"),
    ("This article is not certified by peer review.", "submitted"),
    ("Studies that were not peer-reviewed were excluded from the review.", "unknown"),
    ("A preprint version of this article is available on bioRxiv.", "unknown"),
])
def test_first_page_markers_require_self_reference(text: str, expected: str) -> None:
    assert infer_version("https://publisher.example/paper", pdf_text=text)[0] == expected


def test_library_runs_a_bounded_number_of_queries(conn: sqlite3.Connection) -> None:
    from litforge.ui.api import library, run_results

    conn.executemany("INSERT INTO papers(id,title,doi,created_at,updated_at) VALUES (?,?,?,'now','now')",
                     [(f"q{i}", f"Paper {i}", f"10.1234/q{i}") for i in range(2000)])
    conn.executemany("INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,version,status,created_at) "
                     "VALUES (?,?,?,'x.pdf',1,'submitted','ok','now')",
                     [(f"a{i}", f"q{i}", f"sha{i}") for i in range(0, 2000, 2)])
    _group(conn, "g", {"q0": "published", "q1": "preprint"})
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    rows = library(conn)
    conn.set_trace_callback(None)
    assert len(rows) == 1999 and len(statements) <= 10
    row = next(r for r in rows if r["id"] == "q0")
    assert (row["version"], row["version_role"], row["primary"]) == ("preprint", "published", True)
    assert "versions" not in row
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('pr','pr','now')")
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,params_json,stats_json,status,started_at) "
                 "VALUES ('run','pr','search','{}','{}','done','now')")
    conn.executemany("INSERT INTO project_papers(project_id,paper_id,source,added_at) VALUES ('pr',?,'d','now')",
                     [(f"q{i}",) for i in range(300)])
    conn.executemany("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project,reasons_json,"
                     "components_json) VALUES ('run',?,1,1,1,'[]','{}')", [(f"q{i}",) for i in range(300)])
    statements.clear()
    conn.set_trace_callback(statements.append)
    papers = run_results(conn, "run")["papers"]
    conn.set_trace_callback(None)
    assert len(papers) == 299
    assert sum("FROM artifacts WHERE paper_id=?" in s or "paper_versions WHERE paper_id=?" in s
               for s in statements) == 0


def test_unlink_remembers_rejection_and_link_runs_do_not_relink(conn: sqlite3.Connection) -> None:
    from litforge.discovery.versions import unlink_version, versions_for_paper

    _paper(conn, "published", "10.7554/elife.106194")
    _paper(conn, "preprint", "10.1101/2024.01.01.123456")
    relations = {"10.7554/elife.106194": [{"id-type": "doi", "id": "10.1101/2024.01.01.123456",
                                           "relation": "has-preprint"}]}
    assert link_explicit_versions(conn, relations) == 1
    assert unlink_version(conn, "preprint")
    assert primary_paper_ids(conn) == {"published", "preprint"}
    assert versions_for_paper(conn, "preprint") == []
    assert link_explicit_versions(conn, relations) == 0
    assert primary_paper_ids(conn) == {"published", "preprint"}
    assert not unlink_version(conn, "preprint")


def test_merging_groups_keeps_the_only_explicit_primary_and_refreshes_roles(conn: sqlite3.Connection) -> None:
    _paper(conn, "a", "10.7554/elife.1", openalex="W1")
    _paper(conn, "b", "10.1101/2024.01.01.111111", openalex="W1")
    _paper(conn, "c", "10.5555/c", openalex="W2")
    _paper(conn, "d", "10.1101/2024.01.01.222222", openalex="W2")
    assert link_explicit_versions(conn) == 2
    set_primary(conn, "b")
    conn.execute("UPDATE paper_versions SET role='unknown' WHERE paper_id='a'")  # stale stored role
    link_explicit_versions(conn, {"10.7554/elife.1": [{"id-type": "doi", "id": "10.5555/c",
                                                       "relation": "is-version-of"}]})
    assert primary_paper_ids(conn) == {"b"}
    assert conn.execute("SELECT role FROM paper_versions WHERE paper_id='a'").fetchone()[0] == "published"


def test_confirming_a_suggestion_closes_it(conn: sqlite3.Connection) -> None:
    from litforge.discovery.versions import confirm_version_link, suggest_similar_versions, versions_for_paper

    _paper(conn, "a", "10.1234/a")
    _paper(conn, "b", "10.1234/b")
    conn.execute("UPDATE papers SET title='A long study of neural dynamics and rhythms' WHERE id IN ('a','b')")
    assert suggest_similar_versions(conn) == 1
    assert confirm_version_link(conn, "a", "b")
    assert [v["confirmed"] for v in versions_for_paper(conn, "a")] == [1, 1]
    assert suggest_similar_versions(conn) == 0


def test_link_versions_can_be_scoped_to_papers(conn: sqlite3.Connection) -> None:
    _paper(conn, "published", "10.7554/elife.106194")
    _paper(conn, "other", "10.5555/other")
    transport = FakeTransport([("api.crossref.org/works/", (404, {}, b"{}"))])
    link_versions(conn, HttpClient(transport=transport, sleep=lambda _: None), paper_ids=["other"])
    assert transport.calls and all("10.5555/other" in call for call in transport.calls)


def test_backfill_skips_missing_pdfs_and_ignores_cited_dois(conn: sqlite3.Connection, paths: Paths) -> None:
    from litforge.discovery.versions import backfill_unknown_versions

    _paper(conn, "own", "10.5555/own")
    _paper(conn, "cited", "10.5555/cited")
    _paper(conn, "gone", "10.5555/gone")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "DOI: 10.5555/own\nReferences: [1] doi:10.5555/cited")
    doc.save(paths.root / "own.pdf")
    doc.close()
    ts = now()
    conn.execute("INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at) "
                 "VALUES ('own-pdf','own','own-sha','own.pdf',1,'ok',?)", (ts,))
    conn.execute("INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at) "
                 "VALUES ('gone-pdf','gone','gone-sha','gone.pdf',1,'ok',?)", (ts,))
    assert backfill_unknown_versions(conn, paths) == (0, 1)
    assert conn.execute("SELECT version_label FROM artifacts WHERE id='own-pdf'").fetchone()[0] is None


def test_cli_versions_unlink(conn: sqlite3.Connection, paths: Paths) -> None:
    from typer.testing import CliRunner

    from litforge.cli import app

    _paper(conn, "published", "10.7554/elife.106194")
    _paper(conn, "preprint", "10.1101/2024.01.01.123456")
    link_explicit_versions(conn, {"10.7554/elife.106194": [
        {"id-type": "doi", "id": "10.1101/2024.01.01.123456", "relation": "has-preprint"}]})
    conn.commit()
    runner = CliRunner()
    result = runner.invoke(app, ["--data-dir", str(paths.root), "versions", "unlink", "preprint"])
    assert result.exit_code == 0, result.output
    assert primary_paper_ids(conn) == {"published", "preprint"}
    assert runner.invoke(app, ["--data-dir", str(paths.root), "versions", "unlink", "preprint"]).exit_code != 0
