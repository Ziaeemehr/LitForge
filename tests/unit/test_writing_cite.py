from __future__ import annotations

import json
from pathlib import Path

import pytest
from lens_fakes import seed

from litforge.config import Paths
from litforge.storage.db import open_db
from litforge.writing import cite as wc
from litforge.writing import documents as wd
from litforge.writing.bib import BEGIN_MARK, END_MARK, citation_key


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    paths.ensure()
    conn = open_db(paths.db)
    span = seed(conn, "p1", "Results: the gain was 3.25 in 40 subjects.")
    seed(conn, "p2", "Results: no effect of noise.")
    conn.execute("UPDATE papers SET authors_json=?, year=2021 WHERE id='p1'", (json.dumps(["Ann Lee"]),))
    conn.execute("UPDATE papers SET authors_json=?, year=2019 WHERE id='p2'", (json.dumps(["Bo Kim"]),))
    doc = wd.create_document(conn, paths, "demo", "Draft")
    yield conn, paths, doc, span
    conn.close()


def test_cited_keys_parses_variants_and_ignores_comments() -> None:
    text = "\\cite{a, b} \\citep[p.~2]{c}\\parencite*{a}\n% \\cite{hidden}\n\\nocite{d} 50\\% \\cite{e}"
    assert wc.cited_keys(text) == ["a", "b", "c", "d", "e"]


def test_search_library_by_title_author_year_doi(env) -> None:
    conn, paths, doc, _ = env
    assert [r["paper_id"] for r in wc.search_library(conn, doc.project_id, "lee")] == ["p1"]
    assert [r["paper_id"] for r in wc.search_library(conn, doc.project_id, "2019")] == ["p2"]
    assert len(wc.search_library(conn, doc.project_id, "title")) == 2
    assert wc.search_library(conn, doc.project_id, "100%") == []


def test_search_evidence_returns_quotes_with_pages(env) -> None:
    conn, paths, doc, _ = env
    results = wc.search_evidence(conn, doc.project_id, "gain subjects")
    assert results[0]["paper_id"] == "p1" and results[0]["page"] == 1 and results[0]["evidence_id"].startswith("ev_")


def test_cite_writes_bib_and_records_evidence(env) -> None:
    conn, paths, doc, span = env
    bib = doc.folder / "references.bib"
    bib.write_text("@book{mine,\n  title = {Mine}\n}\n", encoding="utf-8")
    result = wc.cite(conn, doc, "p1", span.id)
    assert result["key"] == citation_key(conn, "p1")
    text = bib.read_text(encoding="utf-8")
    assert text.startswith("@book{mine,") and result["key"] in text
    quotes = wc.quotes_for_key(conn, doc, result["key"])
    assert quotes["quotes"][0]["evidence_id"] == span.id and quotes["quotes"][0]["page"] == 1
    with pytest.raises(ValueError):
        wc.cite(conn, doc, "p2", span.id)  # evidence from another paper


def test_sync_keeps_user_entries_and_unknown_keys(env) -> None:
    conn, paths, doc, _ = env
    key1, key2 = citation_key(conn, "p1"), citation_key(conn, "p2")
    bib = doc.folder / "references.bib"
    bib.write_text(f"@misc{{{key2},\n  note = {{my own}}\n}}\n", encoding="utf-8")
    (doc.folder / "main.tex").write_text(f"\\cite{{{key1},unknownkey,{key2}}}\n", encoding="utf-8")
    wc.sync_bib(conn, doc)
    text = bib.read_text(encoding="utf-8")
    managed = text.split(BEGIN_MARK)[1].split(END_MARK)[0]
    assert key1 in managed and key2 not in managed and "unknownkey" not in text
    assert "note = {my own}" in text


def test_cite_refuses_papers_outside_the_project(env) -> None:
    conn, paths, doc, _ = env
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('px','Other','now','now')")
    with pytest.raises(LookupError):
        wc.cite(conn, doc, "px")


def test_cited_keys_more_forms() -> None:
    text = "\\Citet{d} \\cites{e}{f} x\\\\% \\cite{g}\n\\cite[see][p.~2]{a} 5\\% \\citeauthor*{b}"
    assert wc.cited_keys(text) == ["d", "e", "f", "a", "b"]


def test_sync_bib_refuses_a_references_bib_that_links_outside(env, tmp_path: Path) -> None:
    conn, paths, doc, _ = env
    secret = tmp_path / "secret.bib"
    secret.write_text("@misc{secret, note = {private}}\n")
    bib = doc.folder / "references.bib"
    bib.unlink()
    bib.symlink_to(secret)
    with pytest.raises(wd.DocumentError):
        wc.sync_bib(conn, doc, ["p1"])
    assert bib.is_symlink() and secret.read_text() == "@misc{secret, note = {private}}\n"
