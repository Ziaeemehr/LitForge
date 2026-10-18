"""Explainable active-learning ordering; predictions never change decisions."""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from litforge.analysis.claims import paper_runs
from litforge.discovery.textsim import tfidf_similarity
from litforge.discovery.versions import primary_paper_ids
from litforge.screening.criteria import load_criteria


class QueueItem(BaseModel):
    paper_id: str
    title: str
    model_decision: str | None
    stage: str
    predicted_relevant: float | None
    uncertainty: float
    disagreement: float
    boundary: float
    coverage: float
    info: float
    reason: str


def review_queue(conn: sqlite3.Connection, project_id: str, limit: int = 20,
                 all_versions: bool = False) -> list[QueueItem]:
    load_criteria(conn, project_id)
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = conn.execute("SELECT p.id,p.title,p.abstract,m.mark,s.decision AS s1,s.score AS score1,"
                        "f.decision AS s2,f.score AS score2 FROM project_papers pp JOIN papers p ON p.id=pp.paper_id "
                        "LEFT JOIN paper_marks m ON m.project_id=pp.project_id AND m.paper_id=p.id "
                        "LEFT JOIN screening_results s ON s.project_id=pp.project_id AND s.paper_id=p.id "
                        "LEFT JOIN fulltext_screening f ON f.project_id=pp.project_id AND f.paper_id=p.id "
                        "WHERE pp.project_id=? ORDER BY p.id", (project_id,)).fetchall()
    primary = None if all_versions else primary_paper_ids(conn, project_id)
    rows = [r for r in rows if primary is None or r["id"] in primary]
    marked = [r for r in rows if r["mark"] in ("relevant", "irrelevant")]
    texts = [f"{r['title'] or ''} {r['abstract'] or ''}" for r in marked]
    runs = paper_runs(conn, project_id)
    queue = []
    for row in rows:
        if row["mark"]:
            continue
        sims = tfidf_similarity(f"{row['title'] or ''} {row['abstract'] or ''}", texts)
        neighbors = sorted(zip(sims, marked, strict=True), key=lambda pair: (-pair[0], pair[1]["id"]))
        nearest = neighbors[:5]
        total = sum(s for s, _ in nearest)
        predicted = sum(s for s, r in nearest if r["mark"] == "relevant") / total if total else None
        pos = [s for s, r in neighbors if r["mark"] == "relevant"][:5]
        neg = [s for s, r in neighbors if r["mark"] == "irrelevant"][:5]
        boundary = min(sum(pos) / len(pos) if pos else 0, sum(neg) / len(neg) if neg else 0)
        use_s2 = row["s2"] is not None and row["id"] in runs
        score = row["score2"] if use_s2 else row["score1"]
        decision = row["s2"] if use_s2 else row["s1"]
        uncertainty = 1 - abs(2 * max(0, min(1, score)) - 1) if score is not None else 1.0
        disagreement = float(bool((use_s2 and row["s1"] and row["s1"] != row["s2"]) or
                             (predicted is not None and ((decision == "include" and predicted < .5) or
                                                         (decision == "exclude" and predicted > .5)))))
        coverage = (float(row["id"] not in runs) + float(not (row["abstract"] or "").strip())) / 2
        info = .4 * uncertainty + .3 * disagreement + .2 * boundary + .1 * coverage
        reasons = []
        if uncertainty > .5:
            reasons.append("Uncertain model score" if decision else "Not screened")
        if disagreement:
            reasons.append("Screening stages or nearby human marks disagree")
        if boundary:
            reasons.append("Similar to both relevant and irrelevant papers")
        if row["id"] not in runs:
            reasons.append("Needs PDF / current parse")
        if not (row["abstract"] or "").strip():
            reasons.append("No abstract")
        queue.append(QueueItem(paper_id=row["id"], title=row["title"] or row["id"], model_decision=decision,
                               stage="S2" if use_s2 else "S1", predicted_relevant=predicted,
                               uncertainty=uncertainty, disagreement=disagreement, boundary=boundary,
                               coverage=coverage, info=info, reason="; ".join(reasons) or "Routine review"))
    return sorted(queue, key=lambda item: (-item.info, item.paper_id))[:limit]
