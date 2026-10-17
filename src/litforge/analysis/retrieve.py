"""Lexical retrieval for natural-language research questions."""

from __future__ import annotations

import sqlite3

from litforge.indexing.fts import SearchHit, search


def retrieve_for_question(conn: sqlite3.Connection, question: str, project_id: str | None = None,
                          k: int = 8, max_per_paper: int = 3) -> list[SearchHit]:
    if k <= 0 or max_per_paper <= 0:
        return []
    # Expand the candidate pool until content fills k slots or all matches are seen.
    # Apply diversity after demoting front matter so title hits cannot consume it.
    limit = k
    while True:
        hits = search(conn, question, project_id, limit, limit, any_terms=True)
        content = [h for h in hits if h.section != "Front matter"]
        front = [h for h in hits if h.section == "Front matter"]
        selected: list[SearchHit] = []
        per_paper: dict[str, int] = {}
        for hit in content + front:
            if per_paper.get(hit.paper_id, 0) >= max_per_paper:
                continue
            selected.append(hit)
            per_paper[hit.paper_id] = per_paper.get(hit.paper_id, 0) + 1
            if len(selected) == k:
                break
        if len(hits) < limit or (len(selected) == k and all(h.section != "Front matter" for h in selected)):
            return selected
        limit *= 2
