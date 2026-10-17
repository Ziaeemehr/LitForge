"""Data access for the web UI: projects, runs, results with statistics, paper details, marks.

Kept free of HTTP concerns so it can be tested directly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from litforge.analysis import ask as answers
from litforge.analysis import audit as audits
from litforge.analysis import claim_graph, claims
from litforge.analysis import matrix as matrices
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.ledger import get_spans
from litforge.analysis.review import review_queue
from litforge.config import Config, Paths
from litforge.export.markdown import default_export_dir
from litforge.parsing.canonical import read_canonical, to_markdown
from litforge.parsing.vision import read_page_markdown
from litforge.projects import MARK_SCOPES, get_project_id
from litforge.screening import active, fulltext
from litforge.screening.criteria import Criteria, criteria_hash, load_criteria, save_criteria
from litforge.screening.history import append_history
from litforge.storage.db import now

MARKS = ("relevant", "irrelevant")


def list_projects(conn: sqlite3.Connection, paths: Paths) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.id, p.name, p.topic, p.created_at, COUNT(pp.paper_id) AS papers,
                  (SELECT COUNT(*) FROM discovery_runs r WHERE r.project_id = p.id) AS runs
           FROM projects p LEFT JOIN project_papers pp ON pp.project_id = p.id
           GROUP BY p.id ORDER BY p.created_at DESC"""
    ).fetchall()
    return [{**dict(r), "export_dir": str(default_export_dir(conn, paths, r["name"], r["id"]).expanduser().resolve())}
            for r in rows]


def set_export_dir(conn: sqlite3.Connection, project_id: str, export_dir: str | None) -> None:
    conn.execute("UPDATE projects SET export_dir=? WHERE id=?", (export_dir or None, project_id))


def list_runs(conn: sqlite3.Connection, project_id: str | None, limit: int = 50) -> list[dict[str, Any]]:
    """Runs of one project, or of all projects (newest first) for the sidebar and Recents page."""
    rows = conn.execute(
        f"""SELECT r.id, r.kind, r.status, r.params_json, r.stats_json, r.started_at, p.name AS project,
                  (SELECT COUNT(*) FROM discovery_results d WHERE d.run_id = r.id) AS papers
           FROM discovery_runs r JOIN projects p ON p.id = r.project_id
           {"WHERE r.project_id = ?" if project_id else ""} ORDER BY r.rowid DESC LIMIT ?""",
        (project_id, limit) if project_id else (limit,),
    ).fetchall()
    out = []
    for r in rows:
        params = json.loads(r["params_json"])
        label = _run_label(conn, r["kind"], params)
        out.append({"id": r["id"], "kind": r["kind"], "status": r["status"], "started_at": r["started_at"],
                    "papers": r["papers"], "label": str(label)[:80], "project": r["project"]})
    return out


def library(conn: sqlite3.Connection, limit: int = 5000) -> list[dict[str, Any]]:
    """Every paper in the library with its PDF status and the projects it belongs to."""
    rows = conn.execute(
        """SELECT p.id, p.title, p.year, p.venue, p.doi, p.arxiv_id, p.oa_url, p.authors_json, p.cited_by_count,
                  EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status = 'ok') AS has_pdf,
                  (SELECT GROUP_CONCAT(pr.name, '|') FROM project_papers pp JOIN projects pr ON pr.id = pp.project_id
                   WHERE pp.paper_id = p.id) AS projects
           FROM papers p ORDER BY has_pdf DESC, p.year DESC, p.title LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{
        "id": r["id"], "title": r["title"], "year": r["year"], "venue": r["venue"], "doi": r["doi"],
        "arxiv_id": r["arxiv_id"], "oa_url": r["oa_url"], "cited_by_count": r["cited_by_count"],
        "authors": (json.loads(r["authors_json"]) if r["authors_json"] else [])[:4], "has_pdf": bool(r["has_pdf"]),
        "projects": sorted(r["projects"].split("|")) if r["projects"] else [],
    } for r in rows]


def missing_pdfs(conn: sqlite3.Connection, project_id: str, only: str = "all") -> list[dict[str, Any]]:
    """A project's papers that have no PDF in the library yet, with the links for getting one by hand.
    `only` filters by the marks, as for export."""
    if only not in MARK_SCOPES:
        raise ValueError(f"only must be one of {sorted(MARK_SCOPES)}")
    rows = conn.execute(
        f"""SELECT p.id, p.title, p.year, p.venue, p.doi, p.arxiv_id, p.oa_url, p.authors_json, m.mark
           FROM project_papers pp JOIN papers p ON p.id = pp.paper_id
           LEFT JOIN paper_marks m ON m.paper_id = p.id AND m.project_id = pp.project_id
           WHERE pp.project_id = ?{MARK_SCOPES[only]}
             AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status = 'ok')
           ORDER BY m.mark = 'relevant' DESC, p.year DESC, p.title""",
        (project_id,),
    ).fetchall()
    return [{**{k: r[k] for k in ("id", "title", "year", "venue", "doi", "arxiv_id", "oa_url", "mark")},
             "authors": (json.loads(r["authors_json"]) if r["authors_json"] else [])[:3]} for r in rows]


def _seed_label(conn: sqlite3.Connection, seed: str) -> str:
    """Show a seed by the title of its paper when known: a paper id, a DOI or arXiv id, or an uploaded
    PDF (named <sha256 prefix>-<file name> by the upload handler); otherwise by the file name or id."""
    row = conn.execute("SELECT title FROM papers WHERE id=?", (seed,)).fetchone()
    if seed.lower().endswith(".pdf"):
        name = Path(seed).name
        if m := re.match(r"([0-9a-f]{12})-(.+)$", name):
            row = conn.execute("SELECT p.title FROM artifacts a JOIN papers p ON p.id = a.paper_id"
                               " WHERE a.sha256 LIKE ? LIMIT 1", (m.group(1) + "%",)).fetchone()
            name = m.group(2)
        return str(row["title"]) if row and row["title"] else name
    if row is None:
        bare = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:|arxiv:)", "", seed.strip(), flags=re.I)
        row = conn.execute("SELECT title FROM papers WHERE lower(doi) = lower(?) OR arxiv_id = ? LIMIT 1",
                           (bare, bare)).fetchone()
    return str(row["title"]) if row and row["title"] else seed


def _run_label(conn: sqlite3.Connection, kind: str, params: dict[str, Any]) -> str:
    seeds = params.get("seeds") or []
    if seeds:
        return _seed_label(conn, seeds[0]) + (f" + {len(seeds) - 1} more" if len(seeds) > 1 else "")
    if params.get("file"):
        return Path(params["file"]).name
    return str(params.get("title") or params.get("abstract") or ", ".join(params.get("queries") or []) or kind)


def run_results(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = conn.execute("SELECT r.*, p.name AS project FROM discovery_runs r JOIN projects p ON p.id = r.project_id"
                       " WHERE r.id=?", (run_id,)).fetchone()
    if run is None:
        raise LookupError("run not found")
    rows = conn.execute(
        """SELECT d.paper_id, d.score, d.n_hits, d.new_to_project, d.reasons_json, d.components_json,
                  COALESCE(d.importance, 0) AS importance, COALESCE(d.priority, d.score) AS priority,
                  p.is_retracted, p.citation_percentile,
                  p.title, p.year, p.venue, p.doi, p.arxiv_id, p.openalex_id, p.oa_url, p.cited_by_count,
                  p.authors_json,
                  EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status='ok') AS has_pdf,
                  m.mark, s.decision AS s_decision, s.score AS s_score, s.result_json AS s_result,
                  s.error AS s_error, s.criteria_hash AS s_hash
           FROM discovery_results d JOIN papers p ON p.id = d.paper_id
           LEFT JOIN paper_marks m ON m.paper_id = p.id AND m.project_id = ?
           LEFT JOIN screening_results s ON s.paper_id = p.id AND s.project_id = ?
           WHERE d.run_id = ? ORDER BY priority DESC, d.score DESC, p.title""",
        (run["project_id"], run["project_id"], run_id),
    ).fetchall()
    current = criteria_hash(load_criteria(conn, run["project_id"]))
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
            "screening": None if r["s_decision"] is None else {
                "decision": r["s_decision"], "score": r["s_score"], "error": r["s_error"],
                "reason": json.loads(r["s_result"]).get("reason", ""), "stale": r["s_hash"] != current},
        })
    queue = {i.paper_id: i.model_dump() for i in active.review_queue(conn, run["project_id"], 2**31 - 1)}
    for paper in papers:
        paper["fulltext_screening"] = fulltext_result(conn, run["project_id"], paper["id"])
        paper["review_queue"] = queue.get(paper["id"])
    params = json.loads(run["params_json"])
    label = _run_label(conn, run["kind"], params)
    return {"run": {**dict(run), "params": params, "label": label, "stats": json.loads(run["stats_json"])},
            "papers": papers, "stats": result_stats(papers)}


def result_stats(papers: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary numbers and small distributions for the results header."""
    years = [p["year"] for p in papers if p["year"]]
    channel_counts: Counter[str] = Counter(c for p in papers for c in p["channels"] if c != "seed")
    venues: Counter[str] = Counter(p["venue"] for p in papers if p["venue"])
    screened = Counter(p["screening"]["decision"] for p in papers if p.get("screening"))
    return {
        "screened": sum(screened.values()),
        "screen_include": screened["include"],
        "screen_uncertain": screened["uncertain"],
        "screen_exclude": screened["exclude"],
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
            markdown = to_markdown(read_canonical(out_dir), None, asset_prefix=f"/api/papers/{paper_id}/files/",
                                   page_md=read_page_markdown(out_dir))
    mark = None
    if project_id:
        m = conn.execute("SELECT mark FROM paper_marks WHERE project_id=? AND paper_id=?",
                         (project_id, paper_id)).fetchone()
        mark = m["mark"] if m else None
    screening = None
    if project_id:
        s = conn.execute("SELECT * FROM screening_results WHERE project_id=? AND paper_id=?",
                         (project_id, paper_id)).fetchone()
        if s:
            screening = {**json.loads(s["result_json"]), "model": s["model"], "screened_at": s["screened_at"],
                         "stale": s["criteria_hash"] != criteria_hash(load_criteria(conn, project_id))}
    cites = conn.execute("SELECT COUNT(*) FROM paper_relations WHERE citing_id=?", (paper_id,)).fetchone()[0]
    cited = conn.execute("SELECT COUNT(*) FROM paper_relations WHERE cited_id=?", (paper_id,)).fetchone()[0]
    return {
        "id": row["id"], "title": row["title"], "year": row["year"], "venue": row["venue"], "doi": row["doi"],
        "doi_status": row["doi_status"], "arxiv_id": row["arxiv_id"], "openalex_id": row["openalex_id"],
        "oa_url": row["oa_url"], "cited_by_count": row["cited_by_count"], "abstract": row["abstract"],
        "authors": json.loads(row["authors_json"]) if row["authors_json"] else [],
        "has_pdf": run is not None, "markdown": markdown, "mark": mark,
        "pdf_url": f"/api/papers/{paper_id}/pdf" if _pdf_path(conn, paths, paper_id) else None,
        "links_in_library": {"cites": cites, "cited_by": cited}, "screening": screening,
        "fulltext_screening": fulltext_result(conn, project_id, paper_id) if project_id else None,
        "screening_history": [dict(r) for r in conn.execute(
            "SELECT * FROM screening_history WHERE project_id=? AND paper_id=? ORDER BY rowid",
            (project_id, paper_id))] if project_id else [],
    }


def _pdf_path(conn: sqlite3.Connection, paths: Paths, paper_id: str) -> Path | None:
    row = conn.execute("SELECT pdf_path FROM artifacts WHERE paper_id=? AND status='ok' ORDER BY created_at LIMIT 1",
                       (paper_id,)).fetchone()
    path = paths.root / row["pdf_path"] if row else None
    return path if path is not None and path.is_file() else None


def paper_pdf(conn: sqlite3.Connection, paths: Paths, paper_id: str) -> tuple[Path, str]:
    """The paper's PDF in the library, with a readable download name."""
    path = _pdf_path(conn, paths, paper_id)
    if path is None:
        raise LookupError("no PDF for this paper")
    title = conn.execute("SELECT title FROM papers WHERE id=?", (paper_id,)).fetchone()
    name = re.sub(r"[^\w\- ]+", "", (title["title"] if title and title["title"] else paper_id))[:80].strip()
    return path, f"{name or 'paper'}.pdf"


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


def set_marks(conn: sqlite3.Connection, project: str, paper_ids: list[str], mark: str | None) -> None:
    """Set (or clear, with None) the same mark on several papers at once."""
    project_id = get_project_id(conn, project)
    if mark is not None and mark not in MARKS:
        raise ValueError(f"mark must be one of {MARKS}")
    chash = criteria_hash(load_criteria(conn, project_id))
    for pid in dict.fromkeys(paper_ids):
        append_history(conn, project_id, pid, "S3", {"relevant": "include", "irrelevant": "exclude"}.get(
            mark or "", "uncertain"), "human", chash, "Mark cleared" if mark is None else "Human mark: " + mark)
    if mark is None:
        conn.executemany("DELETE FROM paper_marks WHERE project_id=? AND paper_id=?",
                         [(project_id, pid) for pid in paper_ids])
        return
    if mark not in MARKS:
        raise ValueError(f"mark must be one of {MARKS}")
    stamp = now()
    conn.executemany(
        """INSERT INTO paper_marks(project_id, paper_id, mark, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(project_id, paper_id) DO UPDATE SET mark=excluded.mark, updated_at=excluded.updated_at""",
        [(project_id, pid, mark, stamp) for pid in paper_ids],
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


def delete_run(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """Undo a run: remove it from the history and take its papers out of the project, unless they matter
    elsewhere: marked ✓/✗, have a PDF, found by another remaining run, or added outside discovery (e.g.
    `ingest`). Deleting runs in any order thus leaves exactly what the remaining runs found. Library
    records stay, so a later run that finds them again needs no new lookups."""
    run = conn.execute("SELECT project_id, status FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError("run not found")
    if run["status"] == "running":
        raise ValueError("this run is still running")
    added = [r["paper_id"] for r in conn.execute(
        """SELECT d.paper_id FROM discovery_results d
           JOIN project_papers pp ON pp.paper_id = d.paper_id AND pp.project_id = ?
           WHERE d.run_id = ? AND pp.source = 'discovery'""", (run["project_id"], run_id))]
    keep = {r[0] for r in conn.execute(
        f"""SELECT p.id FROM papers p WHERE p.id IN ({",".join("?" * len(added))}) AND (
              EXISTS (SELECT 1 FROM paper_marks m WHERE m.paper_id = p.id AND m.project_id = ?)
              OR EXISTS (SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status = 'ok')
              OR EXISTS (SELECT 1 FROM discovery_results o JOIN discovery_runs r ON r.id = o.run_id
                         WHERE o.paper_id = p.id AND r.project_id = ? AND r.id != ?))""",
        [*added, run["project_id"], run["project_id"], run_id],
    )} if added else set()
    removed = [pid for pid in added if pid not in keep]
    conn.executemany("DELETE FROM project_papers WHERE project_id=? AND paper_id=?",
                     [(run["project_id"], pid) for pid in removed])
    conn.execute("DELETE FROM discovery_runs WHERE id=?", (run_id,))  # hits and results cascade
    return {"removed": len(removed), "kept": len(keep)}


def get_criteria(conn: sqlite3.Connection, project: str) -> dict[str, Any]:
    return load_criteria(conn, get_project_id(conn, project)).model_dump()


def put_criteria(conn: sqlite3.Connection, project: str, body: dict[str, Any]) -> dict[str, Any]:
    """Save the screening form; include/exclude come as text (one criterion per line) or lists."""
    def text(value: Any) -> str:
        return "\n".join(map(str, value)) if isinstance(value, list) else str(value or "")

    c = Criteria.from_text(str(body.get("question") or ""), text(body.get("include")), text(body.get("exclude")))
    save_criteria(conn, get_project_id(conn, project), c)
    return c.model_dump()


# Evidence Lens adapters: validation and presentation only; analysis owns all model work.
class LensRequest(BaseModel):
    task: Literal['ask', 'audit', 'matrix_extract', 'claim_extract', 'claim_relation', 'screen_fulltext']
    project: str | None = None
    question: str = ''
    run_id: str = ''
    matrix_id: str = ''
    k: int = Field(default=8, ge=1, le=100)
    profile: str | None = None
    approved: bool = Field(default=False, strict=True)
    sensitive: bool = False
    force: bool = False
    paper_ids: list[str] | None = None
    max_pairs: int = Field(default=200, ge=1)
    top: int | None = Field(default=None, ge=1)


def lens_plan(conn: sqlite3.Connection, gateway: ModelGateway, request: LensRequest) -> dict[str, Any]:
    if request.task == 'ask':
        if not request.question.strip():
            raise ValueError('give a question')
        pid = get_project_id(conn, request.project) if request.project else None
        calls = answers.plan_calls(conn, gateway, request.question, pid, request.k,
                                   request.profile, request.sensitive)
    elif request.task == 'audit':
        calls = audits.plan_calls(conn, gateway, request.run_id, request.profile)
    elif request.task in ("claim_extract", "claim_relation", "screen_fulltext"):
        if not request.project:
            raise ValueError("choose a project")
        pid = get_project_id(conn, request.project)
        if request.task == "claim_extract":
            calls = claims.plan_calls(conn, gateway, pid, request.paper_ids, request.profile, request.force)
        elif request.task == "claim_relation":
            calls = claim_graph.plan_calls(conn, gateway, pid, request.max_pairs, request.profile, request.force)
        else:
            calls = fulltext.plan_calls(conn, gateway, pid, request.top, request.profile,
                                        request.force, request.paper_ids)
    else:
        calls = matrices.plan_calls(conn, gateway, request.matrix_id, request.paper_ids,
                                    request.profile, request.force)
    return {
        'profiles': sorted({p.profile for p in calls}), 'providers': sorted({p.provider for p in calls}),
        'models': sorted({p.model for p in calls}), 'remote': any(p.remote for p in calls),
        'number_of_calls': len(calls), 'evidence_ids_count': len({e for p in calls for e in p.evidence_ids}),
        'estimated_input_tokens': sum(p.estimated_input_tokens for p in calls),
        'max_cost_usd': None if any(p.max_cost_usd is None for p in calls) else
                        sum(p.max_cost_usd or 0 for p in calls),
        'requires_approval': any(p.remote or p.requires_approval for p in calls),
        'blocked_reasons': sorted({p.blocked_reason for p in calls if p.blocked_reason}),
    }


def lens_answer(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    answer = answers.get_answer(conn, run_id)
    evidence = {s.id: s for s in answer.evidence}
    data = answer.model_dump()
    data['sentences'] = [{**s.model_dump(), 'needs_review': s.needs_review,
                          'audit_focus': s.needs_review or len({evidence[e].paper_id for e in s.evidence_ids
                                                               if e in evidence}) < 2}
                         for s in answer.sentences]
    return data


def lens_matrix(conn: sqlite3.Connection, matrix_id: str) -> dict[str, Any]:
    matrix = matrices.get_matrix(conn, matrix_id)
    data = matrix.model_dump()
    data['warnings'] = matrices.matrix_warnings(matrices.MatrixResult(run_id="", matrix=matrix, cells=matrix.cells))
    data['evidence'] = [s.model_dump() for s in matrices.matrix_spans(conn, matrix)]
    data['papers'] = [dict(r) for r in conn.execute(
        'SELECT p.id,p.title FROM papers p WHERE (? IS NULL OR p.id IN '
        '(SELECT paper_id FROM project_papers WHERE project_id=?)) ORDER BY p.title',
        (matrix.project_id, matrix.project_id))]
    return data


def lens_reviews(conn: sqlite3.Connection, project_id: str | None) -> list[dict[str, Any]]:
    items = []
    for item in review_queue(conn, project_id):
        table, context = (('answer_sentences', 'run_id') if item.target_kind == 'sentence'
                          else ('matrix_cells', 'matrix_id'))
        row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (item.target_id,)).fetchone()
        pages = {r['evidence_id']: r['page'] for r in conn.execute(
            'SELECT evidence_id,page FROM run_evidence WHERE run_id=? AND page IS NOT NULL', (row['run_id'],))}
        items.append({**item.model_dump(), context: row[context],
                      'evidence': [{**s.model_dump(), 'page': pages.get(s.id, s.page)}
                                   for s in get_spans(conn, json.loads(row['evidence_ids_json']))]})
    return items


class MatrixRequest(BaseModel):
    project: str | None = None
    name: str
    question: str
    columns: list[matrices.MatrixColumn]


class ReviewRequest(BaseModel):
    target_kind: Literal['sentence', 'cell']
    target_id: str
    action: Literal['accept', 'reject', 'edit']
    new_value: Any = None
    reason: str = ''


def model_profiles(config: Config) -> list[dict[str, Any]]:
    """Public profile choices, with local profiles before cloud profiles."""
    profiles = {**config.models.profiles, "local": config.models.local}
    return [{"name": name, "provider": p.provider, "model": p.model, "remote": p.remote}
            for name, p in sorted(profiles.items(), key=lambda item: (item[1].remote, item[0] != "local", item[0]))]


def fulltext_result(conn: sqlite3.Connection, project_id: str, paper_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM fulltext_screening WHERE project_id=? AND paper_id=?",
                       (project_id, paper_id)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["result"] = json.loads(row["result_json"])
    data["stale"] = row["criteria_hash"] != criteria_hash(load_criteria(conn, project_id))
    pages = {r["evidence_id"]: r["page"] for r in conn.execute(
        "SELECT evidence_id,page FROM run_evidence WHERE run_id=?", (row["run_id"],))}
    data["evidence"] = [{**s.model_dump(), "page": pages.get(s.id) or s.page}
                        for s in get_spans(conn, json.loads(row["evidence_ids_json"]))]
    return data


def lens_claims(conn: sqlite3.Connection, project_id: str, cluster_id: str | None = None) -> dict[str, Any]:
    if cluster_id and not conn.execute("SELECT 1 FROM claim_clusters WHERE id=? AND project_id=?",
                                       (cluster_id, project_id)).fetchone():
        raise KeyError(cluster_id)
    members = claims.list_claims(conn, project_id, cluster_id)
    ids = {c.id for c in members}
    clusters = claim_graph.claim_landscape(conn, project_id)
    paper_ids = {c.paper_id for c in members}
    for cluster in clusters:
        paper_ids.update(cluster.papers)
        paper_ids.update(cluster.later_citing_papers)
    papers = {r["id"]: {"title": r["title"], "year": r["year"]} for r in conn.execute(
        "SELECT id,title,year FROM papers WHERE id IN (" + ",".join("?" for _ in paper_ids) + ")",
        sorted(paper_ids))}
    return {"claims": [c.model_dump() for c in members], "papers": papers,
            "clusters": [c.model_dump() for c in clusters],
            "relations": [r.model_dump() for r in claim_graph.list_relations(conn, project_id)
                          if r.source_claim_id in ids and r.target_claim_id in ids],
            "evidence": list({s.id: s.model_dump() for c in members for s in claims.claim_spans(conn, c)}.values())}


class ClaimReviewRequest(BaseModel):
    id: str
    action: Literal["accept", "reject", "edit"]
    text: str | None = None
    reason: str = ""


class SnapshotRequest(BaseModel):
    project: str = Field(min_length=1)
    label: str = ""


class RefreshRequest(BaseModel):
    project: str = Field(min_length=1)
    screen: bool = Field(default=True, strict=True)
    extract: bool = Field(default=True, strict=True)
    claims: bool = Field(default=True, strict=True)


def living_panel(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    from litforge.analysis.living import delta_markdown, get_watch_run
    from litforge.analysis.snapshots import list_snapshots
    items = list_snapshots(conn, project_id)
    row = conn.execute("SELECT id FROM watch_runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                       (project_id,)).fetchone()
    latest = get_watch_run(conn, row[0]) if row else None
    return {"snapshots": [{**s.model_dump(exclude={"data"}), "counts": {
                "papers": len(s.data.papers), "matrix": len(s.data.matrix),
                "claims": len(s.data.claims), "answers": len(s.data.answers)}} for s in items],
            "latest": latest.model_dump() if latest else None,
            "report": delta_markdown(latest.delta, latest.signals) if latest and latest.delta else ""}
