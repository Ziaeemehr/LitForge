"""End-to-end acceptance tests for step 1: ingest → parse → index → search → export."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pymupdf
import pytest
from typer.testing import CliRunner

from litforge.cli import app
from litforge.config import Config, Paths
from litforge.export.markdown import export_markdown
from litforge.indexing.fts import current_runs, index_run, search
from litforge.ingest import ingest_paths
from litforge.parsing.canonical import read_canonical
from litforge.parsing.run import artifacts_for_project, parse_artifact
from litforge.projects import create_project


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("papers", "artifacts", "artifact_aliases")
    )


@pytest.fixture()
def project(conn: sqlite3.Connection, paths: Paths) -> str:
    return create_project(conn, paths, "demo", topic="test")


def _ingest_parse_index(conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path, pid: str) -> None:
    ingest_paths(conn, paths, [corpus], pid)
    for art in artifacts_for_project(conn, pid):
        parse_artifact(conn, paths, config, art)
    for run in current_runs(conn, pid):
        index_run(conn, paths, config, run)


def _artifact_by_alias(conn: sqlite3.Connection, filename: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT a.* FROM artifacts a JOIN artifact_aliases al ON al.artifact_id = a.id WHERE al.filename=?",
        (filename,),
    ).fetchone()


def test_ingest_dedups_quarantines_and_is_idempotent(
    conn: sqlite3.Connection, paths: Paths, corpus: Path, project: str
) -> None:
    results = ingest_paths(conn, paths, [corpus], project)
    by_name = {Path(r.path).name: r for r in results}
    new = {n for n, r in by_name.items() if r.status == "new"}
    assert {"two_column.pdf", "image_only.pdf"} <= new and len(new) == 3
    # Whichever copy of the same bytes is seen first is new; the other is a duplicate.
    twins = {by_name["single_column.pdf"].status, by_name["same_bytes_other_name.pdf"].status}
    assert twins == {"new", "duplicate"}
    assert {n for n, r in by_name.items() if r.status == "quarantined"} == {
        "encrypted.pdf", "corrupt.pdf", "not_a_pdf.pdf"}
    assert "encrypted" in (by_name["encrypted.pdf"].message or "")

    before = _counts(conn)
    again = ingest_paths(conn, paths, [corpus], project)
    assert all(r.status == "duplicate" for r in again)
    assert _counts(conn) == before

    # Originals are preserved (including quarantined ones) under their content hash.
    for r in results:
        assert r.sha256 and (paths.pdfs / f"{r.sha256}.pdf").is_file()
    # Duplicate bytes share one artifact with two filename aliases.
    art = _artifact_by_alias(conn, "same_bytes_other_name.pdf")
    names = {r["filename"] for r in conn.execute(
        "SELECT filename FROM artifact_aliases WHERE artifact_id=?", (art["id"],))}
    assert names == {"single_column.pdf", "same_bytes_other_name.pdf"}


def test_metadata_is_extracted_with_sources(
    conn: sqlite3.Connection, paths: Paths, corpus: Path, project: str
) -> None:
    ingest_paths(conn, paths, [corpus], project)
    art = _artifact_by_alias(conn, "single_column.pdf")
    paper = conn.execute("SELECT * FROM papers WHERE id=?", (art["paper_id"],)).fetchone()
    assert paper["title"] == "Inferring Neural Mass Parameters"
    assert paper["doi"] == "10.1234/synthetic.5678" and paper["doi_status"] == "unverified"
    assert paper["year"] is None  # never guessed
    assert paper["abstract"] and paper["abstract"].startswith("Neural mass models")
    meta = json.loads(paper["metadata_json"])
    assert meta["title"]["source"] == "page1_largest_font"
    assert meta["authors"]["value"] == ["Ada Lovelace", "Alan Turing"]


def test_parse_structure_routing_and_cache(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path, project: str
) -> None:
    ingest_paths(conn, paths, [corpus], project)
    single = _artifact_by_alias(conn, "single_column.pdf")
    first = parse_artifact(conn, paths, config, single)
    assert (first.outcome, first.status) == ("parsed", "ok")

    run = conn.execute("SELECT * FROM parse_runs WHERE artifact_id=? AND is_current=1", (single["id"],)).fetchone()
    doc = read_canonical(paths.root / run["output_dir"])
    headings = [(s.heading, s.kind) for s in doc.sections if s.heading]
    assert headings == [("Abstract", "abstract"), ("1 Introduction", "body"), ("2 Methods", "body"),
                        ("3 Results", "body"), ("References", "references")]
    text = doc.full_text()
    assert "Journal of Synthetic Tests" not in text  # running header removed
    assert "Journal of Synthetic Tests, Vol. 1" in doc.furniture
    intro = next(s for s in doc.sections if s.heading == "1 Introduction")
    assert [p.page for p in intro.paragraphs] == [1, 1]
    assert intro.paragraphs[0].text.startswith("PARA-INTRO-ONE")

    kinds = {a.kind: a for a in doc.assets}
    assert kinds["figure"].label == "Figure 1" and kinds["figure"].page == 2 and kinds["figure"].path
    assert kinds["table"].label == "Table 1"
    assert kinds["table"].data and kinds["table"].data["cells"][1] == ["gain A", "3.25", "0.10"]
    assert kinds["equation"].label == "Eq. (1)"
    out_dir = paths.root / run["output_dir"]
    assert (out_dir / kinds["figure"].path).is_file()  # type: ignore[operator]
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE parse_run_id=?", (run["id"],)).fetchone()[0] == 3

    # Cache: second run is served from cache; --force re-parses.
    assert parse_artifact(conn, paths, config, single).outcome == "cached"
    assert parse_artifact(conn, paths, config, single, force=True).outcome == "parsed"
    assert conn.execute("SELECT COUNT(*) FROM parse_runs WHERE artifact_id=?", (single["id"],)).fetchone()[0] == 1

    image_only = _artifact_by_alias(conn, "image_only.pdf")
    res = parse_artifact(conn, paths, config, image_only)
    assert res.status == "needs_ocr"  # empty output is never a valid success


def test_two_column_reading_order(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path, project: str
) -> None:
    ingest_paths(conn, paths, [corpus], project)
    art = _artifact_by_alias(conn, "two_column.pdf")
    parse_artifact(conn, paths, config, art)
    run = conn.execute("SELECT * FROM parse_runs WHERE artifact_id=?", (art["id"],)).fetchone()
    doc = read_canonical(paths.root / run["output_dir"])
    markers = re.findall(r"(LEFTCOL\d|RIGHTCOL\d)", doc.full_text())
    assert markers == [f"LEFTCOL{i}" for i in range(1, 5)] + [f"RIGHTCOL{i}" for i in range(1, 5)]
    assert doc.pages[0].column_count == 2


def test_search_hits_point_to_pages_containing_the_quote(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path, project: str
) -> None:
    _ingest_parse_index(conn, paths, config, corpus, project)
    hits = search(conn, "zebrafinch", project)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.title == "Inferring Neural Mass Parameters" and hit.section == "3 Results"
    pdf = conn.execute("SELECT pdf_path FROM artifacts WHERE paper_id=?", (hit.paper_id,)).fetchone()[0]
    with pymupdf.open(paths.root / pdf) as doc:
        assert "zebrafinch" in doc[hit.page - 1].get_text()
    # Every hit for a common term is verifiable on its cited page.
    for h in search(conn, "posterior", project, limit=20):
        pdf = conn.execute("SELECT pdf_path FROM artifacts WHERE paper_id=?", (h.paper_id,)).fetchone()[0]
        with pymupdf.open(paths.root / pdf) as doc:
            assert "posterior" in doc[h.page - 1].get_text().lower()
    # References are not indexed by default; table content is (via its caption chunk).
    assert search(conn, "REFERENCE-ONLY-TOKEN", project) == []
    table_hits = search(conn, "gain", project)
    assert any(h.kind == "caption" and h.page == 2 for h in table_hits)


def test_search_is_scoped_to_project(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path, project: str
) -> None:
    _ingest_parse_index(conn, paths, config, corpus, project)
    other = create_project(conn, paths, "other")
    assert search(conn, "zebrafinch", other) == []
    assert len(search(conn, "zebrafinch", None)) == 1


def test_export_markdown_has_relative_working_links(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path, project: str
) -> None:
    _ingest_parse_index(conn, paths, config, corpus, project)
    root = export_markdown(conn, paths, "demo", project)
    readme = (root / "README.md").read_text()
    assert "Inferring Neural Mass Parameters" in readme
    for md in (root / "knowledge_base").glob("*.md"):
        text = md.read_text()
        assert text.startswith("---\npaper_id:")
        for link in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
            assert not link.startswith("/")
            assert (md.parent / link).resolve().is_file(), link


def test_cli_end_to_end(tmp_path: Path, corpus: Path) -> None:
    runner = CliRunner()
    base = ["--data-dir", str(tmp_path / "data")]
    for args in (
        ["project", "create", "demo", "--topic", "t"],
        ["ingest", str(corpus), "-p", "demo"],
        ["parse", "-p", "demo"],
        ["index", "-p", "demo"],
        ["status", "-p", "demo"],
        ["export", "-p", "demo"],
    ):
        result = runner.invoke(app, base + args)
        assert result.exit_code == 0, (args, result.output)
    result = runner.invoke(app, base + ["search", "zebrafinch", "-p", "demo", "--json"])
    assert result.exit_code == 0
    hits = json.loads(result.output)
    assert hits[0]["page"] == 3
    again = runner.invoke(app, base + ["parse", "-p", "demo", "--json"])
    assert {r["outcome"] for r in json.loads(again.output)} == {"cached"}
    missing = runner.invoke(app, base + ["parse", "-p", "nope"])
    assert missing.exit_code == 2
