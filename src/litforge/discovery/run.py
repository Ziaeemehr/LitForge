"""Discovery runs: multi-source search, citation chaining, reference import and enrichment.

Every run is recorded (parameters, per-source counts, errors, API cost) and every hit keeps the
reason it was found, so searches can be re-run and compared.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from litforge.discovery.http import BudgetExhausted, HttpClient, HttpError
from litforge.discovery.importance import fetch_missing_venues, importance_scores, priority
from litforge.discovery.importers import read_references
from litforge.discovery.merge import apply_record, ensure_norm_titles, upsert_record
from litforge.discovery.rank import rank
from litforge.discovery.records import WorkRecord, normalize_arxiv_id
from litforge.discovery.sources import Arxiv, Crossref, OpenAlex, SearchSource
from litforge.discovery.versions import link_versions, suggest_similar_versions
from litforge.metadata.extract import normalize_doi, normalize_title
from litforge.projects import add_paper
from litforge.storage.db import dumps, new_id, now, transaction

SOURCE_ERRORS = (HttpError, BudgetExhausted, urllib.error.URLError, TimeoutError, ValueError)
Direction = Literal["references", "cited_by", "both"]


class RankedPaper(BaseModel):
    paper_id: str
    title: str | None
    year: int | None
    venue: str | None
    doi: str | None
    arxiv_id: str | None
    score: float
    sources: list[str]
    new_to_project: bool
    has_pdf: bool
    oa_url: str | None
    reasons: list[str] = []
    importance: float = 0.0
    priority: float = 0.0
    importance_notes: list[str] = []


class DiscoveryReport(BaseModel):
    run_id: str
    kind: str
    status: str
    hits: int = 0
    papers: int = 0
    created: int = 0
    new_to_project: int = 0
    per_source: dict[str, int] = {}
    errors: list[str] = []
    requests: int = 0
    cache_hits: int = 0
    cost_usd: float = 0.0
    top: list[RankedPaper] = []


class Sources:
    """Factory for adapters sharing one HttpClient."""

    def __init__(self, http: HttpClient, openalex_key: str | None = None, mailto: str | None = None) -> None:
        self.http = http
        self.openalex = OpenAlex(http, openalex_key, mailto)
        self.crossref = Crossref(http, mailto)
        self.arxiv = Arxiv(http)

    def by_name(self, name: str) -> SearchSource:
        adapters: dict[str, SearchSource] = {"openalex": self.openalex, "crossref": self.crossref,
                                             "arxiv": self.arxiv}
        if name not in adapters:
            raise ValueError(f"unknown source '{name}' (available: {', '.join(adapters)})")
        return adapters[name]


class _Run:
    """Collects hits for one discovery run and persists them."""

    def __init__(self, conn: sqlite3.Connection, project_id: str, kind: str, params: dict[str, object]) -> None:
        self.conn = conn
        self.project_id = project_id
        self.report = DiscoveryReport(run_id=new_id(), kind=kind, status="running")
        self.hits: list[tuple[str, str, str | None, int]] = []
        self.queries: list[str] = [str(q) for q in params.get("queries", []) or []]  # type: ignore[attr-defined]
        self.new_ids: set[str] = set()
        ensure_norm_titles(conn)
        conn.execute(
            "INSERT INTO discovery_runs(id, project_id, kind, params_json, status, started_at) VALUES (?,?,?,?,?,?)",
            (self.report.run_id, project_id, kind, dumps(params), "running", now()),
        )

    def add(self, records: Sequence[WorkRecord], source: str, relation: str, query: str | None = None,
            seed_paper_id: str | None = None) -> list[str]:
        ids = []
        ts = now()
        with transaction(self.conn):
            for position, rec in enumerate(records, 1):
                paper_id, created = upsert_record(self.conn, rec, ts)
                self.report.created += created
                if add_paper(self.conn, self.project_id, paper_id, "discovery"):
                    self.new_ids.add(paper_id)
                self.conn.execute(
                    """INSERT INTO discovery_hits(run_id, paper_id, source, source_id, query, relation,
                       seed_paper_id, rank) VALUES (?,?,?,?,?,?,?,?)""",
                    (self.report.run_id, paper_id, source, rec.source_id, query, relation, seed_paper_id, position),
                )
                self.hits.append((paper_id, source, query, position))
                ids.append(paper_id)
        self.report.per_source[source] = self.report.per_source.get(source, 0) + len(records)
        self.report.hits += len(records)
        return ids

    def add_papers(self, paper_ids: Sequence[str], source: str, relation: str, new_ids: set[str]) -> None:
        """Record papers already in the library and project (e.g. from PDFs the user added)."""
        with transaction(self.conn):
            for position, paper_id in enumerate(paper_ids, 1):
                self.conn.execute(
                    """INSERT INTO discovery_hits(run_id, paper_id, source, source_id, query, relation,
                       seed_paper_id, rank) VALUES (?,?,?,?,?,?,?,?)""",
                    (self.report.run_id, paper_id, source, None, None, relation, None, position),
                )
                self.hits.append((paper_id, source, None, position))
        self.new_ids |= new_ids & set(paper_ids)
        self.report.per_source[source] = self.report.per_source.get(source, 0) + len(paper_ids)
        self.report.hits += len(paper_ids)

    def finish(
        self,
        http: HttpClient | None,
        top_n: int,
        scores: dict[str, float] | None = None,
        reasons: dict[str, list[str]] | None = None,
        components: dict[str, dict[str, float]] | None = None,
        openalex: OpenAlex | None = None,
    ) -> DiscoveryReport:
        """Persist the ranking (default: lexical + RRF; callers may pass their own scores)."""
        wanted = sorted({h[0] for h in self.hits} | set(scores or {}))
        rows = {r["id"]: r for r in self.conn.execute(
            f"""SELECT p.id, p.title, p.abstract, p.year, p.venue, p.doi, p.arxiv_id, p.oa_url,
                       EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status = 'ok') AS has_pdf
                FROM papers p WHERE p.id IN ({",".join("?" * len(wanted))})""",
            wanted,
        )} if wanted else {}
        if scores is None:
            scores = rank(self.hits, {pid: (r["title"], r["abstract"]) for pid, r in rows.items()}, self.queries)
        reasons = reasons or {}
        components = components or {}
        with transaction(self.conn):
            if openalex is not None:
                fetch_missing_venues(self.conn, openalex, list(scores))
        imp = importance_scores(self.conn, list(scores))
        prio = {pid: priority(score, imp.get(pid, {}).get("score", 0.0)) for pid, score in scores.items()}
        sources: dict[str, set[str]] = {}
        for paper_id, source, _q, _pos in self.hits:
            sources.setdefault(paper_id, set()).add(source)
        for paper_id in scores:
            sources.setdefault(paper_id, set())
        report = self.report
        with transaction(self.conn):
            for paper_id, score in scores.items():
                info = imp.get(paper_id, {"score": 0.0, "components": {}, "notes": []})
                comp = {**components.get(paper_id, {}), "importance": info["components"], "notes": info["notes"]}
                self.conn.execute(
                    """INSERT INTO discovery_results(run_id, paper_id, score, n_hits, new_to_project, reasons_json,
                       components_json, importance, priority) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (report.run_id, paper_id, score, sum(1 for h in self.hits if h[0] == paper_id),
                     int(paper_id in self.new_ids), dumps(reasons.get(paper_id, [])), dumps(comp),
                     info["score"], prio[paper_id]),
                )
            report.papers = len(scores)
            report.new_to_project = len(self.new_ids)
            if http is not None:
                report.requests, report.cache_hits = http.requests_made, http.cache_hits
                report.cost_usd = round(http.cost_usd, 4)
            report.status = "partial" if report.errors and report.hits else "failed" if report.errors else "done"
            self.conn.execute(
                "UPDATE discovery_runs SET status=?, stats_json=?, finished_at=? WHERE id=?",
                (report.status, dumps(report.model_dump(exclude={"top"})), now(), report.run_id),
            )
        ordered = sorted(scores.items(), key=lambda kv: (-prio[kv[0]], -kv[1], rows[kv[0]]["title"] or ""))
        report.top = [
            RankedPaper(paper_id=pid, title=rows[pid]["title"], year=rows[pid]["year"], venue=rows[pid]["venue"],
                        doi=rows[pid]["doi"], arxiv_id=rows[pid]["arxiv_id"], score=score,
                        sources=sorted(sources[pid]), new_to_project=pid in self.new_ids,
                        has_pdf=bool(rows[pid]["has_pdf"]), oa_url=rows[pid]["oa_url"],
                        reasons=reasons.get(pid, []), importance=imp.get(pid, {}).get("score", 0.0),
                        priority=prio[pid], importance_notes=imp.get(pid, {}).get("notes", []))
            for pid, score in ordered[:top_n]
        ]
        return report

    def error(self, where: str, exc: Exception) -> None:
        self.report.errors.append(f"{where}: {type(exc).__name__}: {exc}"[:300])


def run_search(
    conn: sqlite3.Connection, sources: Sources, project_id: str, queries: list[str], source_names: list[str],
    limit: int, top_n: int = 20, progress: Callable[[str], None] | None = None,
) -> DiscoveryReport:
    run = _Run(conn, project_id, "search", {"queries": queries, "sources": source_names, "limit": limit})
    for name in source_names:
        try:
            adapter = sources.by_name(name)
        except ValueError as exc:
            run.error(name, exc)
            continue
        for query in queries:
            if progress:
                progress(f"searching {name}: {query}")
            try:
                records = adapter.search(query, limit)
            except SOURCE_ERRORS as exc:
                run.error(f"{name} '{query}'", exc)
                if isinstance(exc, BudgetExhausted):
                    break
                continue
            run.add(records, name, "query", query)
    return run.finish(sources.http, top_n, openalex=sources.openalex)


def _resolve_seed(conn: sqlite3.Connection, sources: Sources, seed: str) -> WorkRecord | None:
    """seed: DOI, arXiv id, OpenAlex id, or a LitForge paper id."""
    row = conn.execute("SELECT openalex_id, doi, arxiv_id, title FROM papers WHERE id=?", (seed,)).fetchone()
    if row is not None:
        for ident in (row["openalex_id"], row["doi"], row["arxiv_id"]):
            if ident and (rec := sources.openalex.get(ident)):
                return rec
        if row["title"]:
            for rec in sources.openalex.title_lookup(row["title"]):
                if rec.title and normalize_title(rec.title) == normalize_title(row["title"]):
                    return rec
        return None
    return sources.openalex.get(seed)


def run_citations(
    conn: sqlite3.Connection, sources: Sources, project_id: str, seeds: list[str], direction: Direction,
    limit: int, top_n: int = 20, topic: str | None = None,
) -> DiscoveryReport:
    params: dict[str, object] = {"seeds": seeds, "direction": direction, "limit": limit,
                                 "queries": [topic] if topic else []}
    run = _Run(conn, project_id, "citations", params)
    for seed in seeds:
        try:
            seed_rec = _resolve_seed(conn, sources, seed)
        except SOURCE_ERRORS as exc:
            run.error(f"seed {seed}", exc)
            continue
        if seed_rec is None or not seed_rec.openalex_id:
            run.error(f"seed {seed}", LookupError("not found in OpenAlex"))
            continue
        seed_id = run.add([seed_rec], "openalex", "seed")[0]
        ts = now()
        for rel in ("references", "cited_by"):
            if direction not in (rel, "both"):
                continue
            try:
                records = (sources.openalex.references if rel == "references" else sources.openalex.citing)(
                    seed_rec.openalex_id, limit)
            except SOURCE_ERRORS as exc:
                run.error(f"{rel} of {seed}", exc)
                continue
            ids = run.add(records, "openalex", rel, seed_paper_id=seed_id)
            record_relations(conn, seed_id, ids, rel, ts)
    return run.finish(sources.http, top_n, openalex=sources.openalex)


def record_relations(conn: sqlite3.Connection, seed_id: str, ids: list[str], rel: str, ts: str) -> None:
    """rel='references': seed cites ids; rel='cited_by': ids cite seed."""
    with transaction(conn):
        for other in ids:
            citing, cited = (seed_id, other) if rel == "references" else (other, seed_id)
            conn.execute(
                "INSERT OR IGNORE INTO paper_relations(citing_id, cited_id, source, created_at) VALUES (?,?,?,?)",
                (citing, cited, "openalex", ts),
            )


def run_import(conn: sqlite3.Connection, project_id: str, path: Path, top_n: int = 20) -> DiscoveryReport:
    run = _Run(conn, project_id, "import", {"file": str(path)})
    try:
        records = read_references(path)
    except (OSError, ValueError) as exc:
        run.error(path.name, exc)
        return run.finish(None, top_n)
    kept = [r for r in records if r.title or r.doi]
    ids = run.add(kept, f"import:{path.name}", "import")
    # Keep the list's own order as the ranking and its notes (e.g. Elicit "Relevance") as reasons.
    scores = {pid: round(1 - i / max(len(ids), 1), 4) for i, pid in enumerate(dict.fromkeys(ids))}
    label = "Elicit" if path.stem.lower().startswith("elicit") else path.stem[:40]
    reasons = {pid: [f"{label}: {rec.note}"] for pid, rec in zip(ids, kept, strict=True) if rec.note}
    return run.finish(None, top_n, scores, reasons)


class EnrichResult(BaseModel):
    checked: int = 0
    enriched: int = 0
    not_found: int = 0
    errors: list[str] = []
    filled: dict[str, int] = {}
    cost_usd: float = 0.0


def enrich_project(
    conn: sqlite3.Connection, sources: Sources, project_id: str, progress: Callable[[int, int], None] | None = None,
) -> EnrichResult:
    """Complete DOI/year/venue/abstract/ids for project papers from OpenAlex (then arXiv/Crossref)."""
    ensure_norm_titles(conn)
    rows = conn.execute(
        """SELECT p.* FROM papers p JOIN project_papers pp ON pp.paper_id = p.id
           WHERE pp.project_id=? AND (p.openalex_id IS NULL OR p.year IS NULL OR p.doi IS NULL
                                       OR p.doi_status = 'unverified')""",
        (project_id,),
    ).fetchall()
    result = EnrichResult()
    for i, row in enumerate(rows, 1):
        if progress:
            progress(i, len(rows))
        result.checked += 1
        try:
            rec = _lookup(sources, row)
        except SOURCE_ERRORS as exc:
            result.errors.append(f"{row['title'] or row['id']}: {exc}"[:200])
            if isinstance(exc, BudgetExhausted):
                break
            continue
        if rec is None:
            result.not_found += 1
            continue
        with transaction(conn):
            filled = apply_record(conn, row["id"], rec, now())
        if filled:
            result.enriched += 1
            for col in filled:
                result.filled[col] = result.filled.get(col, 0) + 1
    result.cost_usd = round(sources.http.cost_usd, 4)
    if rows:
        link_versions(conn, sources.http, sources.openalex.mailto)
        suggest_similar_versions(conn)
    return result


def _lookup(sources: Sources, row: sqlite3.Row) -> WorkRecord | None:
    if row["openalex_id"] and (rec := sources.openalex.get(row["openalex_id"])):
        return rec
    if row["doi"] and (rec := sources.openalex.get(normalize_doi(row["doi"]))):
        return rec
    if row["arxiv_id"] and (arxiv := normalize_arxiv_id(row["arxiv_id"])):
        if rec := sources.openalex.get(arxiv):
            return rec
        if rec := sources.arxiv.get(arxiv):
            return rec
    if row["title"] and len(normalize_title(row["title"])) >= 20:
        for rec in sources.openalex.title_lookup(row["title"]):
            same_year = rec.year is None or row["year"] is None or abs(rec.year - row["year"]) <= 1
            if rec.title and normalize_title(rec.title) == normalize_title(row["title"]) and same_year:
                return rec
    return None
