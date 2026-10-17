"""Lexical retrieval for natural-language research questions."""

from __future__ import annotations

import sqlite3

from litforge.indexing.fts import SearchHit, search


def retrieve_for_question(conn: sqlite3.Connection, question: str, project_id: str | None = None,
                          k: int = 8, max_per_paper: int = 3) -> list[SearchHit]:
    if k <= 0 or max_per_paper <= 0:
        return []
    return search(conn, question, project_id, k, max_per_paper, any_terms=True)
