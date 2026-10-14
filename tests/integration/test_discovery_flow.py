"""Discovery end to end against canned API responses: search, merge, citations, import, enrich, ingest."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from discovery_fakes import FakeTransport, default_routes
from typer.testing import CliRunner

from litforge import cli
from litforge.config import Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.records import WorkRecord
from litforge.discovery.run import Sources, enrich_project, run_citations, run_import, run_search
from litforge.ingest import ingest_paths
from litforge.projects import create_project


@pytest.fixture()
def sources() -> Sources:
    return Sources(HttpClient(transport=FakeTransport(default_routes()), sleep=lambda s: None, min_interval={}))


@pytest.fixture()
def pid(conn: sqlite3.Connection, paths: Paths) -> str:
    return create_project(conn, paths, "sbi", topic="simulation-based inference")


def _papers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM papers ORDER BY title").fetchall()


def test_search_merges_sources_and_is_repeatable(conn: sqlite3.Connection, sources: Sources, pid: str) -> None:
    report = run_search(conn, sources, pid, ["simulation-based inference"], ["openalex", "crossref", "arxiv"], 10)
    assert report.status == "done" and report.hits == 6
    titles = [p["title"] for p in _papers(conn)]
    # Frontier appears in all three sources but is stored once; APT in OpenAlex + arXiv once.
    assert titles.count("The frontier of simulation-based inference") == 1
    assert len(titles) == 3
    frontier = next(p for p in _papers(conn) if p["title"].startswith("The frontier"))
    assert frontier["doi_status"] == "verified" and frontier["arxiv_id"] == "1911.01429"
    assert frontier["openalex_id"] == "W2984417636" and frontier["oa_url"]
    top = report.top[0]
    assert top.title == "The frontier of simulation-based inference"
    assert top.sources == ["arxiv", "crossref", "openalex"]
    assert report.top[-1].title == "Strong lensing cosmology"  # irrelevant result ranks last
    assert report.cost_usd == pytest.approx(0.002)  # one search + one venue-stats lookup

    again = run_search(conn, sources, pid, ["simulation-based inference"], ["openalex", "crossref", "arxiv"], 10)
    assert again.created == 0 and again.new_to_project == 0 and len(_papers(conn)) == 3
    assert conn.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0] == 2


def test_source_errors_are_reported_not_fatal(conn: sqlite3.Connection, pid: str) -> None:
    routes = [r for r in default_routes() if "crossref" not in r[0]]
    routes.append(("api.crossref.org", (500, {}, b"boom")))
    srcs = Sources(HttpClient(transport=FakeTransport(routes), sleep=lambda s: None, min_interval={}, max_retries=1))
    report = run_search(conn, srcs, pid, ["x"], ["openalex", "crossref", "nosuch"], 5)
    assert report.status == "partial"
    assert any("crossref" in e for e in report.errors) and any("nosuch" in e for e in report.errors)
    assert report.per_source == {"openalex": 2}


def test_citation_chaining_records_direction(conn: sqlite3.Connection, sources: Sources, pid: str) -> None:
    report = run_citations(conn, sources, pid, ["10.1073/pnas.1912789117"], "both", 10)
    assert report.status == "done", report.errors
    ids = {p["title"]: p["id"] for p in _papers(conn)}
    seed = ids["The frontier of simulation-based inference"]
    rels = {(r["citing_id"], r["cited_id"]) for r in conn.execute("SELECT * FROM paper_relations")}
    assert (seed, ids["Automatic Posterior Transformation for Likelihood-Free Inference"]) in rels
    assert (ids["Neural mass model inference with simulation-based methods"], seed) in rels
    relations = {r["relation"] for r in conn.execute("SELECT relation FROM discovery_hits")}
    assert relations == {"seed", "references", "cited_by"}


def test_title_match_merges_but_conflicting_doi_is_flagged(conn: sqlite3.Connection, pid: str, tmp_path: Path) -> None:
    csv = tmp_path / "refs.csv"
    csv.write_text("Title,DOI,Year\nThe frontier of simulation-based inference,,2020\n"
                   "The frontier of simulation-based inference,10.9999/other.version,2020\n")
    report = run_import(conn, pid, csv)
    # The DOI-less row and the DOI row share title and year with no conflicting ids -> one paper.
    assert report.created == 1
    papers = _papers(conn)
    assert len(papers) == 1 and papers[0]["doi"] == "10.9999/other.version"
    # A third record with a different DOI and the same title is a different version: flagged, not merged.
    from litforge.discovery.merge import upsert_record

    _, created = upsert_record(conn, WorkRecord(source="crossref", source_id="z", doi="10.1073/pnas.1912789117",
                                                title="The frontier of simulation-based inference", year=2020), "t")
    assert created
    assert conn.execute("SELECT COUNT(*) FROM duplicate_candidates WHERE reason='title'").fetchone()[0] == 1


def test_import_then_ingest_attaches_pdf(conn: sqlite3.Connection, paths: Paths, pid: str, corpus: Path,
                                         tmp_path: Path) -> None:
    csv = tmp_path / "elicit.csv"
    csv.write_text("Title,Authors,DOI,Year\n"
                   "Inferring Neural Mass Parameters,Ada Lovelace,10.1234/synthetic.5678,2024\n")
    report = run_import(conn, pid, csv)
    assert report.created == 1 and report.top[0].has_pdf is False
    results = ingest_paths(conn, paths, [corpus / "single_column.pdf"], pid)
    assert results[0].message == "attached to an existing library record"
    papers = _papers(conn)
    assert len(papers) == 1 and papers[0]["year"] == 2024  # the imported year is kept, not overwritten
    assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE paper_id=?", (papers[0]["id"],)).fetchone()[0] == 1


def test_enrich_fills_missing_fields(conn: sqlite3.Connection, paths: Paths, sources: Sources, pid: str) -> None:
    from litforge.discovery.merge import upsert_record
    from litforge.projects import add_paper

    title = "Automatic Posterior Transformation for Likelihood-Free Inference"
    paper_id, _ = upsert_record(conn, WorkRecord(source="pdf", source_id="h", arxiv_id="1905.07488", title=title), "t")
    add_paper(conn, pid, paper_id)
    res = enrich_project(conn, sources, pid)
    assert res.enriched == 1 and res.errors == []
    row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    assert row["openalex_id"] == "W2945000001" and row["year"] == 2019 and row["venue"] == "ICML"


def test_cli_discover_and_papers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport(default_routes())
    monkeypatch.setattr(cli, "HttpClient",
                        lambda **kw: HttpClient(**{**kw, "transport": transport, "sleep": lambda s: None,
                                                   "min_interval": {}}))
    runner = CliRunner()
    base = ["--data-dir", str(tmp_path / "data")]
    assert runner.invoke(app := cli.app, base + ["new", "sbi", "--topic", "simulation-based inference"]).exit_code == 0
    result = runner.invoke(app, base + ["discover"])  # no query: uses the project topic
    assert result.exit_code == 0, result.output
    assert "The frontier of simulation-based inference" in result.output
    result = runner.invoke(app, base + ["discover", "--seed", "10.1073/pnas.1912789117", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["kind"] == "citations"
    listing = runner.invoke(app, base + ["papers", "--no-pdf"])
    assert listing.exit_code == 0 and "frontier" in listing.output
    assert runner.invoke(app, base + ["export"]).exit_code == 0


def test_same_generic_title_years_apart_is_not_flagged(conn: sqlite3.Connection) -> None:
    from litforge.discovery.merge import upsert_record

    for doi, year in (("10.1/a", 1993), ("10.1/b", 2009), ("10.1/c", 2010)):
        upsert_record(conn, WorkRecord(source="crossref", source_id=doi, doi=doi, year=year,
                                       title="Simulation-based inference methods"), "t")
    pairs = conn.execute("SELECT COUNT(*) FROM duplicate_candidates").fetchone()[0]
    assert pairs == 1  # only 2009 vs 2010
