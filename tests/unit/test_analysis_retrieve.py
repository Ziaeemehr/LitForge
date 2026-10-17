from __future__ import annotations

import sqlite3

import pytest

from litforge.analysis.retrieve import retrieve_for_question
from litforge.indexing.fts import _hit_page, build_any_match, build_match


@pytest.mark.parametrize(("query", "expected", "terms"), [
    ("How are Neural mass parameters inferred?", '"neural" OR "mass" OR "parameters" OR "inferred"',
     ["neural", "mass", "parameters", "inferred"]),
    ('What is "The Jansen Rit" model model?', '"the jansen rit" OR "model"', ["the jansen rit", "model"]),
    ("the a I x what how ARE", "", []),
    ("neural NEURAL OR mass*", '"neural" OR "mass"', ["neural", "mass"]),
    ('"" ???', "", []),
])
def test_any_match(query: str, expected: str, terms: list[str]) -> None:
    assert build_any_match(query) == (expected, terms)


def test_default_match_unchanged() -> None:
    assert build_match("the neural mass") == ('"the" "neural" "mass"', ["the", "neural", "mass"])


def test_no_content_question(conn: sqlite3.Connection) -> None:
    assert retrieve_for_question(conn, "How is it?") == []


@pytest.mark.parametrize(("query", "page"), [
    ('"neural mass"', 3),
    ('"neural mass" zebrafinch', 2),
    ('"neural mass" missing', 3),
    ('neural', 1),
])
def test_phrase_page_uses_full_match(query: str, page: int) -> None:
    texts = ["neural activity. ", "zebrafinch marker. ", "neural mass model."]
    spans = []
    start = 0
    for number, text in enumerate(texts, 1):
        spans.append({"start": start, "end": start + len(text), "page": number})
        start += len(text)
    _, terms = build_any_match(query)
    assert _hit_page("".join(texts), spans, terms, 1) == page
