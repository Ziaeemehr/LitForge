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


def test_pasted_ligatures_match_spelled_out_index() -> None:
    assert build_any_match("ﬂow eﬃcient") == ('"flow" OR "efficient"', ["flow", "efficient"])
    assert build_match('"normalizing ﬂows"')[0] == '"normalizing flows"'


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


@pytest.mark.parametrize('k,max_per_paper,expected', [
    (2, 3, ['cp', 'cq']), (3, 3, ['cp', 'cq', 'front']),
    (2, 1, ['cp', 'cq']), (0, 3, []),
])
def test_front_matter_follows_content(conn: sqlite3.Connection, k: int, max_per_paper: int,
                                     expected: list[str]) -> None:
    from lens_fakes import seed

    from litforge.indexing.fts import search

    seed(conn, text='Results: gain ' + 'detailed experimental observations ' * 20)
    seed(conn, paper='q', text='Results: gain ' + 'detailed experimental observations ' * 20)
    conn.execute("INSERT INTO chunks(id,parse_run_id,paper_id,kind,section_path,order_idx,text,token_count,"
                 "page_start,page_end,hash) VALUES ('front','rp','p','text','Front matter',1,'gain',1,1,1,'f')")
    assert search(conn, 'gain', any_terms=True)[0].chunk_id == 'front'
    hits = retrieve_for_question(conn, 'gain', k=k, max_per_paper=max_per_paper)
    assert [h.chunk_id for h in hits] == expected
