from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from litforge.storage.db import now, open_db
from litforge.writing.bib import (
    BEGIN_MARK,
    END_MARK,
    CitationKeyError,
    bibtex_entry,
    citation_key,
    set_citation_key,
    write_bib,
)


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "db.sqlite")
    yield c
    c.close()


def add(conn, pid: str, title: str, authors: list[str], year: int | None = 2020, **extra: str) -> str:
    cols = {"id": pid, "title": title, "authors_json": json.dumps(authors), "year": year,
            "created_at": now(), "updated_at": now(), **extra}
    conn.execute(f"INSERT INTO papers({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
    return pid


def balanced(text: str) -> bool:
    depth = 0
    for i, ch in enumerate(text):
        if ch in "{}" and i and text[i - 1] == "\\":
            continue
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def test_key_format_and_stability(conn) -> None:
    add(conn, "p1", "The Virtual Brain: a simulator", ["Paula Sanz Leon", "Viktor Jirsa"])
    key = citation_key(conn, "p1")
    assert key == "leon2020virtual"
    conn.execute("UPDATE papers SET title='Something else', authors_json='[\"Zed Zulu\"]' WHERE id='p1'")
    assert citation_key(conn, "p1") == key


def test_collisions_get_suffixes(conn) -> None:
    for i in range(3):
        add(conn, f"p{i}", "Brain models", ["Ann Smith"])
    assert [citation_key(conn, f"p{i}") for i in range(3)] == ["smith2020brain", "smith2020braina",
                                                               "smith2020brainb"]


def test_non_ascii_names(conn) -> None:
    add(conn, "abcdef12-3456", "مدل مغز", ["علی رضایی"], year=None)
    add(conn, "p2", "Über Gehirne", ["Jürgen Müller"], year=1999)
    assert citation_key(conn, "abcdef12-3456") == "pabcdef12nd"
    assert citation_key(conn, "p2") == "muller1999uber"


def test_set_key_validation(conn) -> None:
    add(conn, "p1", "A", ["X Y"])
    add(conn, "p2", "B", ["X Y"])
    assert set_citation_key(conn, "p1", "mine:1") == "mine:1"
    assert citation_key(conn, "p1") == "mine:1"
    with pytest.raises(CitationKeyError):
        set_citation_key(conn, "p2", "mine:1")
    with pytest.raises(CitationKeyError):
        set_citation_key(conn, "p2", "Bad Key")


def test_entry_escaping_and_fields(conn) -> None:
    add(conn, "p1", "DNA & {braces} 100% of_cases", ["O'Brien, Pat", "Ann Lee"], venue="J. Neuro & Co",
        doi="10.1/x_y", oa_url="https://example.org/a_b?x=1%20")
    entry = bibtex_entry(conn, "p1")
    assert entry.startswith("@article{obrien2020dna,")
    assert r"\&" in entry and r"\%" in entry and r"\{braces\}" in entry and "{DNA}" in entry
    assert "author = {O'Brien, Pat and Lee, Ann}" in entry
    assert "url = {https://example.org/a_b?x=1%20}" in entry
    assert balanced(entry)
    assert re.fullmatch(r"@\w+\{[a-z0-9:_-]+,\n(  \w+ = \{.*\},?\n)+\}\n", entry)


def test_preprint_note(conn) -> None:
    add(conn, "p1", "Fast inference", ["Ann Lee"], arxiv_id="2101.00001", venue="arXiv")
    entry = bibtex_entry(conn, "p1")
    assert entry.startswith("@misc{") and "note = {Preprint}" in entry and "eprint = {2101.00001}" in entry


def test_write_bib_keeps_user_entries(conn, tmp_path: Path) -> None:
    add(conn, "p1", "One", ["Ann Lee"])
    add(conn, "p2", "Two", ["Bob Ray"])
    bib = tmp_path / "references.bib"
    bib.write_text("@book{mine, title={Mine}}\n", encoding="utf-8")
    write_bib(conn, ["p1"], bib)
    write_bib(conn, ["p1", "p2"], bib)
    text = bib.read_text(encoding="utf-8")
    assert text.startswith("@book{mine, title={Mine}}")
    assert text.count(BEGIN_MARK) == 1 and text.count(END_MARK) == 1
    assert "lee2020one" in text and "ray2020two" in text
    assert balanced(text)


def test_deleting_paper_removes_key(conn) -> None:
    add(conn, "p1", "One", ["Ann Lee"])
    citation_key(conn, "p1")
    conn.execute("DELETE FROM papers WHERE id='p1'")
    assert conn.execute("SELECT COUNT(*) FROM citation_keys").fetchone()[0] == 0


def test_cli_export_bibtex(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from litforge import cli
    from litforge.config import Paths
    from litforge.projects import create_project

    paths = Paths(tmp_path)
    paths.ensure()
    c = open_db(paths.db)
    pid = create_project(c, paths, "demo")
    add(c, "p1", "One", ["Ann Lee"])
    c.execute("INSERT INTO project_papers(project_id, paper_id, source, added_at) VALUES (?,?,?,?)",
              (pid, "p1", "manual", now()))
    c.close()
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path), "export", "-p", "demo", "--format", "bibtex"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("@misc{lee2020one,")
