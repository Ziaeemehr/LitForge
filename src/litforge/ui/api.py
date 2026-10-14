"""Data access for the web UI: projects, runs, results with statistics, paper details, marks.

Kept free of HTTP concerns so it can be tested directly.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from litforge.config import Paths
from litforge.export.markdown import default_export_dir
from litforge.parsing.canonical import read_canonical, to_markdown
from litforge.projects import get_project_id
from litforge.storage.db import now

MARKS = ("relevant", "irrelevant")


def list_projects(conn: sqlite3.Connection, paths: Paths) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.id, p.name, p.topic, p.created_at, COUNT(pp.paper_id) AS papers,
                  (SELECT COUNT(*) FROM discovery_runs r WHERE r.project_id = p.id) AS runs
           FROM projects p LEFT JOIN project_papers pp ON pp.project_id = p.id
           GROUP BY p.id ORDER BY p.created_at DESC"""
    ).fetchall()
    return [{**dict(r), "export_dir": str(default_export_dir(conn, paths, r["name"], r["id"]))} for r in rows]


def set_export_dir(conn: sqlite3.Connection, project_id: str, export_dir: str | None) -> None:
    conn.execute("UPDATE projects SET export_dir=? WHERE id=?", (export_dir or None, project_id))


def list_runs(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.id, r.kind, r.status, r.params_json, r.stats_json, r.started_at,
                  (SELECT COUNT(*) FROM discovery_results d WHERE d.run_id = r.id) AS papers
           FROM discovery_runs r WHERE r.project_id=? ORDER BY r.rowid DESC LIMIT 50""",
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        params = json.loads(r["params_json"])
        label = _run_label(conn, r["kind"], params)
        out.append({"id": r["id"], "kind": r["kind"], "status": r["status"], "started_at": r["started_at"],
                    "papers": r["papers"], "label": str(label)[:80]})
    return out


def _seed_label(conn: sqlite3.Connection, seed: str) -> str:
    """Show a paper id as its title and an uploaded file by its name."""
    row = conn.execute("SELECT title FROM papers WHERE id=?", (seed,)).fetchone()
    if row and row["title"]:
        return str(row["title"])
    return Path(seed).name if seed.lower().endswith(".pdf") else seed


def _run_label(conn: sqlite3.Connection, kind: str, params: dict[str, Any]) -> str:
    seeds = params.get("seeds") or []
    if seeds:
        return _seed_label(conn, seeds[0]) + (f" + {len(seeds) - 1} more" if len(seeds) > 1 else "")
    if params.get("file"):
        return Path(params["file"]).name
    return str(params.get("title") or params.get("abstract") or ", ".join(params.get("queries") or []) or kind)


def run_results(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise LookupError("run not found")
    rows = conn.execute(
        """SELECT d.paper_id, d.score, d.n_hits, d.new_to_project, d.reasons_json, d.components_json,
                  COALESCE(d.importance, 0) AS importance, COALESCE(d.priority, d.score) AS priority,
                  p.is_retracted, p.citation_percentile,
                  p.title, p.year, p.venue, p.doi, p.arxiv_id, p.openalex_id, p.oa_url, p.cited_by_count,
                  p.authors_json,
                  EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status='ok') AS has_pdf,
                  m.mark
           FROM discovery_results d JOIN papers p ON p.id = d.paper_id
           LEFT JOIN paper_marks m ON m.paper_id = p.id AND m.project_id = ?
           WHERE d.run_id = ? ORDER BY priority DESC, d.score DESC, p.title""",
        (run["project_id"], run_id),
    ).fetchall()
    channels: dict[str, set[str]] = {}
    sources: dict[str, set[str]] = {}
    for h in conn.execute("SELECT paper_id, relation, source FROM discovery_hits WHERE run_id=?", (run_id,)):
        channels.setdefault(h["paper_id"], set()).add(h["relation"])
        sources.setdefault(h["paper_id"], set()).add(h["source"])
    papers = []
    for r in rows:
        authors = json.loads(r["authors_json"]) if r["authors_json"] else []
        components = json.loads(r["components_json"])
        papers.append({
            "id": r["paper_id"], "score": r["score"], "importance": r["importance"], "priority": r["priority"],
            "impact_notes": components.pop("notes", []), "impact": components.pop("importance", {}),
            "retracted": bool(r["is_retracted"]), "citation_percentile": r["citation_percentile"],
            "title": r["title"], "year": r["year"], "venue": r["venue"],
            "doi": r["doi"], "arxiv_id": r["arxiv_id"], "openalex_id": r["openalex_id"], "oa_url": r["oa_url"],
            "cited_by_count": r["cited_by_count"], "has_pdf": bool(r["has_pdf"]), "mark": r["mark"],
            "new_to_project": bool(r["new_to_project"]), "reasons": json.loads(r["reasons_json"]),
            "components": components, "channels": sorted(channels.get(r["paper_id"], [])),
            "sources": sorted(sources.get(r["paper_id"], [])),
            "authors": authors[:3] + (["et al."] if len(authors) > 3 else []),
        })
    params = json.loads(run["params_json"])
    label = _run_label(conn, run["kind"], params)
    return {"run": {**dict(run), "params": params, "label": label, "stats": json.loads(run["stats_json"])},
            "papers": papers, "stats": result_stats(papers)}


def result_stats(papers: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary numbers and small distributions for the results header."""
    years = [p["year"] for p in papers if p["year"]]
    channel_counts: Counter[str] = Counter(c for p in papers for c in p["channels"] if c != "seed")
    venues: Counter[str] = Counter(p["venue"] for p in papers if p["venue"])
    return {
        "papers": len(papers),
        "new": sum(p["new_to_project"] for p in papers),
        "with_pdf": sum(p["has_pdf"] for p in papers),
        "open_access": sum(bool(p["oa_url"]) for p in papers),
        "relevant": sum(p["mark"] == "relevant" for p in papers),
        "high_impact": sum((p["citation_percentile"] or 0) >= 0.9 for p in papers),
        "retracted": sum(p["retracted"] for p in papers),
        "irrelevant": sum(p["mark"] == "irrelevant" for p in papers),
        "year_min": min(years, default=None),
        "year_max": max(years, default=None),
        "years": sorted(Counter(years).items()),
        "channels": channel_counts.most_common(),
        "venues": venues.most_common(8),
    }


def paper_detail(conn: sqlite3.Connection, paths: Paths, paper_id: str, project_id: str | None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        raise LookupError("paper not found")
    run = conn.execute(
        """SELECT r.output_dir FROM parse_runs r JOIN artifacts a ON a.id = r.artifact_id
           WHERE a.paper_id=? AND a.status='ok' AND r.is_current=1""", (paper_id,)
    ).fetchone()
    markdown = None
    if run and run["output_dir"]:
        out_dir = paths.root / run["output_dir"]
        if (out_dir / "canonical.json").is_file():
            markdown = to_markdown(read_canonical(out_dir), None, asset_prefix=f"/api/papers/{paper_id}/files/")
    mark = None
    if project_id:
        m = conn.execute("SELECT mark FROM paper_marks WHERE project_id=? AND paper_id=?",
                         (project_id, paper_id)).fetchone()
        mark = m["mark"] if m else None
    cites = conn.execute("SELECT COUNT(*) FROM paper_relations WHERE citing_id=?", (paper_id,)).fetchone()[0]
    cited = conn.execute("SELECT COUNT(*) FROM paper_relations WHERE cited_id=?", (paper_id,)).fetchone()[0]
    return {
        "id": row["id"], "title": row["title"], "year": row["year"], "venue": row["venue"], "doi": row["doi"],
        "doi_status": row["doi_status"], "arxiv_id": row["arxiv_id"], "openalex_id": row["openalex_id"],
        "oa_url": row["oa_url"], "cited_by_count": row["cited_by_count"], "abstract": row["abstract"],
        "authors": json.loads(row["authors_json"]) if row["authors_json"] else [],
        "has_pdf": run is not None, "markdown": markdown, "mark": mark,
        "links_in_library": {"cites": cites, "cited_by": cited},
    }


def paper_file(conn: sqlite3.Connection, paths: Paths, paper_id: str, rel: str) -> Path:
    """Resolve a file inside the paper's parse output directory (no path traversal)."""
    run = conn.execute(
        """SELECT r.output_dir FROM parse_runs r JOIN artifacts a ON a.id = r.artifact_id
           WHERE a.paper_id=? AND a.status='ok' AND r.is_current=1""", (paper_id,)
    ).fetchone()
    if run is None or not run["output_dir"]:
        raise LookupError("no parsed output")
    base: Path = (paths.root / str(run["output_dir"])).resolve()
    target = (base / rel).resolve()
    if base not in target.parents or not target.is_file():
        raise LookupError("file not found")
    return target


def set_mark(conn: sqlite3.Connection, project: str, paper_id: str, mark: str | None) -> None:
    project_id = get_project_id(conn, project)
    if mark is None:
        conn.execute("DELETE FROM paper_marks WHERE project_id=? AND paper_id=?", (project_id, paper_id))
        return
    if mark not in MARKS:
        raise ValueError(f"mark must be one of {MARKS}")
    conn.execute(
        """INSERT INTO paper_marks(project_id, paper_id, mark, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(project_id, paper_id) DO UPDATE SET mark=excluded.mark, updated_at=excluded.updated_at""",
        (project_id, paper_id, mark, now()),
    )


def relevant_seeds(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Paper ids marked relevant (used to run 'more like these')."""
    return [r["paper_id"] for r in conn.execute(
        "SELECT paper_id FROM paper_marks WHERE project_id=? AND mark='relevant' ORDER BY updated_at",
        (project_id,))]


def run_paper_ids(conn: sqlite3.Connection, run_id: str, limit: int = 15) -> list[str]:
    """Best-ranked papers of a run (e.g. an imported Elicit list), to use as seeds."""
    return [r["paper_id"] for r in conn.execute(
        "SELECT paper_id FROM discovery_results WHERE run_id=? ORDER BY score DESC LIMIT ?", (run_id, limit))]
