"""Importance of papers in a result set, independent of how relevant they are to the query.

importance (0..1) is a weighted mean of the components that are available for a paper:

- citations  0.35  field- and year-normalized citation percentile (OpenAlex); falls back to the
                   percentile of citations per year within the result set
- centrality 0.30  how many papers of this result set cite it (core literature of the topic)
- momentum   0.20  citations in the last two years (log scale), so recent work is not buried
- venue      0.15  the venue's 2-year mean citedness (impact-factor analogue, log scale)

Retracted papers get importance 0. Review priority = 0.65 * relevance + 0.35 * importance.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from litforge.discovery.http import BudgetExhausted, HttpError
from litforge.discovery.sources import OpenAlex
from litforge.storage.db import now

WEIGHTS = {"citations": 0.35, "centrality": 0.30, "momentum": 0.20, "venue": 0.15}
PRIORITY_WEIGHTS = {"relevance": 0.65, "importance": 0.35}
VENUE_SCALE = 15.0  # mean citedness at which the venue component saturates
MOMENTUM_SCALE = 50.0  # recent citations at which momentum saturates


def fetch_missing_venues(conn: sqlite3.Connection, openalex: OpenAlex, paper_ids: list[str]) -> None:
    """Fetch venue stats once per venue (one cheap request per 50 venues)."""
    if not paper_ids:
        return
    marks = ",".join("?" * len(paper_ids))
    missing = [r["venue_id"] for r in conn.execute(
        f"""SELECT DISTINCT p.venue_id FROM papers p LEFT JOIN venues v ON v.id = p.venue_id
            WHERE p.id IN ({marks}) AND p.venue_id IS NOT NULL AND v.id IS NULL""", paper_ids)]
    if not missing:
        return
    try:
        venues = openalex.venues(missing)
    except (HttpError, BudgetExhausted, OSError, ValueError):
        return  # importance then simply goes without the venue component
    ts = now()
    for v in venues:
        stats = v.get("summary_stats") or {}
        conn.execute(
            "INSERT OR REPLACE INTO venues(id, name, type, mean_citedness, h_index, updated_at) VALUES (?,?,?,?,?,?)",
            ((v.get("id") or "").rsplit("/", 1)[-1], v.get("display_name"), v.get("type"),
             stats.get("2yr_mean_citedness"), stats.get("h_index"), ts),
        )


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.values())
    n = len(ordered)
    if n <= 1:
        return {k: 0.5 for k in values}
    return {k: sum(1 for x in ordered if x < v) / (n - 1) for k, v in values.items()}


def importance_scores(conn: sqlite3.Connection, paper_ids: list[str]) -> dict[str, dict[str, Any]]:
    """paper_id -> {"score": float, "components": {...}, "notes": [...]}."""
    if not paper_ids:
        return {}
    marks = ",".join("?" * len(paper_ids))
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT p.id, p.year, p.cited_by_count, p.citation_percentile, p.fwci, p.recent_citations,
                   p.is_retracted, p.openalex_id, v.mean_citedness, v.name AS venue_name
            FROM papers p LEFT JOIN venues v ON v.id = p.venue_id WHERE p.id IN ({marks})""", paper_ids)}

    # Centrality: citations received from inside this result set.
    by_oa = {r["openalex_id"]: pid for pid, r in rows.items() if r["openalex_id"]}
    cited_in_set: dict[str, int] = {}
    has_refs = conn.execute(
        f"SELECT COUNT(DISTINCT paper_id) FROM paper_references WHERE paper_id IN ({marks})", paper_ids
    ).fetchone()[0]
    if by_oa and has_refs:
        oa_marks = ",".join("?" * len(by_oa))
        for r in conn.execute(
            f"""SELECT ref_id, COUNT(*) AS n FROM paper_references
                WHERE paper_id IN ({marks}) AND ref_id IN ({oa_marks}) GROUP BY ref_id""",
            [*paper_ids, *by_oa],
        ):
            cited_in_set[by_oa[r["ref_id"]]] = r["n"]
    max_central = max(cited_in_set.values(), default=0)

    this_year = datetime.now(UTC).year
    per_year = {pid: (r["cited_by_count"] or 0) / max(1, this_year - (r["year"] or this_year) + 1)
                for pid, r in rows.items() if r["cited_by_count"] is not None}
    fallback_pct = _percentiles(per_year)

    out: dict[str, dict[str, Any]] = {}
    for pid in paper_ids:
        r = rows.get(pid)
        if r is None:
            continue
        comp: dict[str, float | None] = {
            "citations": r["citation_percentile"] if r["citation_percentile"] is not None else fallback_pct.get(pid),
            "centrality": (cited_in_set.get(pid, 0) / max_central) if max_central else None,
            "momentum": (min(1.0, math.log1p(r["recent_citations"]) / math.log1p(MOMENTUM_SCALE))
                         if r["recent_citations"] is not None else None),
            "venue": (min(1.0, math.log1p(r["mean_citedness"]) / math.log1p(VENUE_SCALE))
                      if r["mean_citedness"] is not None else None),
        }
        available = {k: v for k, v in comp.items() if v is not None}
        weight = sum(WEIGHTS[k] for k in available)
        score = sum(WEIGHTS[k] * v for k, v in available.items()) / weight if weight else 0.0
        notes: list[str] = []
        if r["is_retracted"]:
            score = 0.0
            notes.append("RETRACTED")
        pct = r["citation_percentile"]
        if pct is not None and pct >= 0.99:
            notes.append("top 1% cited in its field")
        elif pct is not None and pct >= 0.9:
            notes.append("top 10% cited in its field")
        if cited_in_set.get(pid):
            notes.append(f"cited by {cited_in_set[pid]} paper{'s' if cited_in_set[pid] > 1 else ''} in this set")
        if r["recent_citations"]:
            notes.append(f"{r['recent_citations']} citations in the last two years")
        if r["mean_citedness"] is not None and r["venue_name"]:
            notes.append(f"venue mean citedness {r['mean_citedness']:.1f}")
        out[pid] = {"score": round(score, 4), "components": {k: (round(v, 4) if v is not None else None)
                                                                 for k, v in comp.items()},
                    "notes": notes}
    return out


def priority(relevance: float, importance: float) -> float:
    return round(PRIORITY_WEIGHTS["relevance"] * relevance + PRIORITY_WEIGHTS["importance"] * importance, 4)
