"""Reference exports preserve metadata and the shared final screening decisions."""
import pytest

from litforge.discovery.importers import parse_bibtex, parse_ris
from litforge.writing.bib import bibtex_entry, escape
from tests.unit.test_screening_status import mark, paper, project, result, versions


def export(conn, fmt, **kwargs):
    from litforge.export.references import export_references

    return export_references(conn, "p", fmt, **kwargs)


def seed(conn, title="A study", **metadata):
    project(conn)
    paper(conn, "a")
    conn.execute("UPDATE papers SET title=? WHERE id='a'", (title,))
    for field, value in metadata.items():
        conn.execute(f"UPDATE papers SET {field}=? WHERE id='a'", (value,))
    mark(conn, "a", "relevant")


def test_ris_round_trip_keeps_doi_title_year(conn):
    seed(conn, doi="10.1234/test", year=2024, authors_json='["Ada Lovelace", "Alan Turing"]',
         venue="Journal", abstract="First\r\nSecond", oa_url="https://example.org/paper")
    text = export(conn, "ris")
    row, = parse_ris(text, "test")
    assert (row.doi, row.title, row.year) == ("10.1234/test", "A study", 2024)
    assert row.authors == ["Ada Lovelace", "Alan Turing"]
    assert "\r" not in text
    assert [line[:2] for line in text.splitlines()] == [
        "TY", "TI", "AU", "AU", "PY", "DO", "JO", "AB", "UR", "N1", "KW", "ER"]
    assert "N1  - LitForge: included (S1, human) — included by reviewer\n" in text
    assert "KW  - litforge-included\nER  -\n" in text


def test_bib_round_trip_with_braces_and_persian_title(conn):
    title = "پژوهش {آزمون} & داده"
    seed(conn, title, year=2024, doi="10.1234/test")
    text = export(conn, "bib")
    row, = parse_bibtex(text, "test")
    assert row.title == title
    assert (row.year, row.doi) == (2024, "10.1234/test")
    assert "keywords = {litforge-included}" in text
    assert "note = {LitForge: included (S1, human) — included by reviewer}" in text
    assert export(conn, "bib") == text


@pytest.mark.parametrize("fmt,parser", [("ris", parse_ris), ("bib", parse_bibtex)])
def test_missing_fields_are_omitted_not_empty(conn, fmt, parser):
    seed(conn, "پژوهش")
    text = export(conn, fmt)
    row, = parser(text, "test")
    assert row.title == "پژوهش"
    assert row.year is None and row.doi is None and row.authors == []
    assert not any(line.endswith(" - ") or "= {}" in line for line in text.splitlines())
    if fmt == "ris":
        assert [line[:2] for line in text.splitlines()] == ["TY", "TI", "N1", "KW", "ER"]


def test_status_filter_uses_final_status(conn):
    seed(conn)
    result(conn, "a", "exclude")
    paper(conn, "b")
    result(conn, "b", "include", "S2")
    mark(conn, "b", "irrelevant")
    paper(conn, "c")
    result(conn, "c", "uncertain")
    paper(conn, "d")
    paper(conn, "duplicate")
    versions(conn, "a", "duplicate")
    mark(conn, "duplicate", "relevant")
    assert len(parse_ris(export(conn, "ris"), "test")) == 1
    assert len(parse_ris(export(conn, "ris", status=("included", "excluded", "uncertain", "unscreened")), "test")) == 4
    assert export(conn, "ris", paper_ids=["b", "duplicate"]) == ""
    assert "excluded (S2, human) — excluded by reviewer" in export(conn, "ris", status=["excluded"])
    assert "unscreened (none, none)\n" in export(conn, "ris", status=["unscreened"])
    assert " — " not in export(conn, "ris", status=["unscreened"])


def test_preprint_has_one_combined_escaped_note(conn):
    seed(conn, arxiv_id="2401.12345")
    note = "LitForge: included {reviewer} & checked"
    text = bibtex_entry(conn, "a", extra={"note": note, "keywords": "litforge-included"})
    assert text.count("  note =") == 1
    assert f"note = {{{escape('Preprint. ' + note)}}}" in text
    assert len(parse_bibtex(text, "test")) == 1


def test_export_refs_cli(paths, monkeypatch, tmp_path):
    import json

    from typer.testing import CliRunner

    from litforge.cli import app
    from litforge.projects import create_project
    from litforge.storage.db import open_db

    monkeypatch.setenv("LITFORGE_HOME", str(paths.root))
    conn = open_db(paths.db)
    pid = create_project(conn, paths, "Review")
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('a','A study','now','now')")
    conn.execute("INSERT INTO project_papers VALUES (?,'a','import','now')", (pid,))
    conn.execute("INSERT INTO paper_marks VALUES (?,'a','relevant','now')", (pid,))
    conn.close()
    runner = CliRunner()
    for fmt in ("ris", "bib"):
        response = runner.invoke(app, ["export-refs", "-p", "Review", "--format", fmt, "--json"])
        assert response.exit_code == 0, response.output
        saved = json.loads(response.output)
        assert saved["count"] == 1
        from pathlib import Path

        assert Path(saved["path"]).name == f"references.{fmt}"
        assert "A study" in Path(saved["path"]).read_text()
    target = tmp_path / "custom.ris"
    response = runner.invoke(app, ["export-refs", "-p", "Review", "--format", "ris",
                                   "--status", "excluded", "-o", str(target), "--json"])
    assert response.exit_code == 0, response.output
    assert json.loads(response.output) == {"path": str(target), "count": 0}
    assert target.read_text() == ""
