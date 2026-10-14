"""Deterministic ranking of discovered papers.

score = 0.6 * lexical relevance (query terms in title/abstract) + 0.4 * normalized reciprocal-rank
fusion over all (source, query) result lists. Citation counts are deliberately not used: they
penalize recent work and less-cited fields.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

RRF_K = 60
STOPWORDS = {
    "the", "and", "for", "with", "from", "using", "based", "into", "via", "over", "under", "between",
    "of", "in", "on", "to", "a", "an", "by", "or", "is", "are", "as", "at", "its", "their", "new",
}


def terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower()) if t not in STOPWORDS and len(t) > 2}


def lexical_score(query_terms: set[str], title: str | None, abstract: str | None) -> float:
    if not query_terms:
        return 0.0
    title_terms = terms(title or "")
    abstract_terms = terms(abstract or "")
    hits = sum(2 * (t in title_terms) + (t in abstract_terms) for t in query_terms)
    return hits / (3 * len(query_terms))


def rank(
    hits: Iterable[tuple[str, str, str | None, int]],
    texts: dict[str, tuple[str | None, str | None]],
    queries: list[str],
) -> dict[str, float]:
    """hits: (paper_id, source, query, 1-based rank). texts: paper_id -> (title, abstract)."""
    rrf: dict[str, float] = defaultdict(float)
    for paper_id, _source, _query, position in hits:
        rrf[paper_id] += 1.0 / (RRF_K + position)
    if not rrf:
        return {}
    top = max(rrf.values())
    query_terms = [terms(q) for q in queries if q]
    scores = {}
    for paper_id, fused in rrf.items():
        title, abstract = texts.get(paper_id, (None, None))
        lexical = max((lexical_score(qt, title, abstract) for qt in query_terms), default=0.0)
        weight = 0.6 if query_terms else 0.0
        scores[paper_id] = round(weight * lexical + (1 - weight) * fused / top, 4)
    return scores
