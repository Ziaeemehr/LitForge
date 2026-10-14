"""Find papers similar to one or more seed papers and build a ranked, explainable library.

A seed can be a DOI, arXiv id, OpenAlex id, LitForge paper id, a PDF file, a title, or just an
abstract. Candidates come from several channels, each recorded as the reason a paper was found:

- references / cited_by: the seed's citation neighbourhood (OpenAlex)
- related: OpenAlex "related works"
- keywords: short queries built from the seed's key phrases (OpenAlex + arXiv)
- neighbours: for a text-only seed, the citations of the closest keyword hits

Ranking (deterministic, components stored per paper):
  score = 0.50 * text similarity (embeddings if available, blended with TF-IDF)
        + 0.20 * bibliographic coupling (shared references with the seed)
        + 0.15 * direct citation link with the seed
        + 0.15 * number of independent channels that found the paper
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from litforge.config import Config, Paths
from litforge.discovery.keyphrases import build_queries
from litforge.discovery.records import WorkRecord
from litforge.discovery.run import (
    SOURCE_ERRORS,
    DiscoveryReport,
    Sources,
    _lookup,
    _resolve_seed,
    _Run,
    record_relations,
)
from litforge.discovery.textsim import Embedder, embedding_similarity, tfidf_similarity
from litforge.indexing.fts import current_runs, index_run
from litforge.ingest import ingest_paths
from litforge.metadata.extract import normalize_title
from litforge.observability import NULL_LOG, EventLog
from litforge.parsing.run import parse_artifact
from litforge.storage.db import now

WEIGHTS = {"text": 0.50, "coupling": 0.20, "direct": 0.15, "channels": 0.15}
CHANNELS = ("references", "cited_by", "related", "keywords", "neighbours")
NEIGHBOURS = 3  # stand-in seeds for text-only searches
EMBEDDING_FLOOR = 0.5  # cosine similarity of clearly unrelated texts is ~0.5-0.65 for bge models


@dataclass
class Seed:
    label: str
    title: str | None = None
    abstract: str | None = None
    paper_id: str | None = None
    record: WorkRecord | None = None
    references: set[str] = field(default_factory=set)
    stand_in: bool = False  # a close match standing in for a text-only seed

    @property
    def text(self) -> str:
        return f"{self.title or ''}. {self.abstract or ''}".strip(". ")


def _seed_from_pdf(conn: sqlite3.Connection, paths: Paths, config: Config, sources: Sources, project_id: str,
                   pdf: Path, log: EventLog) -> Seed:
    """Add the PDF to the project (import, parse, index), then look it up in OpenAlex."""
    result = ingest_paths(conn, paths, [pdf], project_id, log=log)[0]
    if result.paper_id is None:
        raise ValueError(f"{pdf.name}: {result.message or result.status}")
    art = conn.execute("SELECT * FROM artifacts WHERE id=?", (result.artifact_id,)).fetchone()
    parse_artifact(conn, paths, config, art, log=log)
    for run in current_runs(conn, project_id):
        if run["artifact_id"] == art["id"]:
            index_run(conn, paths, config, run)
    row = conn.execute("SELECT * FROM papers WHERE id=?", (result.paper_id,)).fetchone()
    rec = _lookup(sources, row)
    return Seed(label=pdf.name, title=(rec.title if rec else None) or row["title"],
                abstract=(rec.abstract if rec else None) or row["abstract"], paper_id=result.paper_id, record=rec)


def _resolve(conn: sqlite3.Connection, paths: Paths, config: Config, sources: Sources, project_id: str,
             identifier: str, log: EventLog) -> Seed:
    path = Path(identifier).expanduser()
    if path.suffix.lower() == ".pdf" and path.is_file():
        return _seed_from_pdf(conn, paths, config, sources, project_id, path, log)
    rec = _resolve_seed(conn, sources, identifier)
    if rec is None:
        raise LookupError(f"'{identifier}' not found (expected a DOI, arXiv id, OpenAlex id, paper id or PDF)")
    return Seed(label=identifier, title=rec.title, abstract=rec.abstract, record=rec)


def _seed_from_text(sources: Sources, title: str | None, abstract: str | None) -> Seed:
    """A title (and/or abstract) given as text; try to find the paper itself for citation signals."""
    seed = Seed(label=(title or abstract or "")[:60], title=title, abstract=abstract)
    if title and len(normalize_title(title)) >= 20:
        for rec in sources.openalex.title_lookup(title):
            if rec.title and normalize_title(rec.title) == normalize_title(title):
                seed.record = rec
                seed.abstract = abstract or rec.abstract
                break
    return seed


def run_similar(
    conn: sqlite3.Connection,
    paths: Paths,
    config: Config,
    sources: Sources,
    project_id: str,
    identifiers: list[str] | None = None,
    title: str | None = None,
    abstract: str | None = None,
    embedder: Embedder | None = None,
    top_n: int = 30,
    log: EventLog = NULL_LOG,
) -> DiscoveryReport:
    cfg = config.similar
    limit = cfg.per_channel_limit
    params: dict[str, object] = {"seeds": identifiers or [], "title": title, "abstract": (abstract or "")[:500],
                                 "embedding_model": embedder.name if embedder else None}
    run = _Run(conn, project_id, "similar", params)

    seeds: list[Seed] = []
    for ident in identifiers or []:
        try:
            seeds.append(_resolve(conn, paths, config, sources, project_id, ident, log))
        except (*SOURCE_ERRORS, LookupError) as exc:
            run.error(f"seed {ident}", exc)
    if title or abstract:
        try:
            seeds.append(_seed_from_text(sources, title, abstract))
        except SOURCE_ERRORS as exc:
            run.error("seed text", exc)
            seeds.append(Seed(label="text", title=title, abstract=abstract))
    if not seeds:
        return run.finish(sources.http, top_n)

    channel_hits: dict[str, set[str]] = {}  # paper_id -> channels
    cand_refs: dict[str, set[str]] = {}  # paper_id -> OpenAlex ids it cites
    direct: dict[str, str] = {}  # paper_id -> "cites the seed" / "cited by the seed"
    keyword_by_paper: dict[str, str] = {}
    seed_ids: set[str] = set()

    def collect(records: list[WorkRecord], ids: list[str], channel: str) -> None:
        for rec, pid in zip(records, ids, strict=True):
            channel_hits.setdefault(pid, set()).add(channel)
            if rec.references:
                cand_refs.setdefault(pid, set()).update(rec.references)

    for seed in seeds:
        if seed.record is not None:
            ids = run.add([seed.record], seed.record.source, "seed")
            seed.paper_id = seed.paper_id or ids[0]
            seed.references = set(seed.record.references)
        if seed.paper_id:
            seed_ids.add(seed.paper_id)
        oa_id = seed.record.openalex_id if seed.record else None
        if oa_id and seed.paper_id:
            ts = now()
            for channel, fetch in (("references", sources.openalex.references), ("cited_by", sources.openalex.citing)):
                try:
                    records = fetch(oa_id, limit)
                except SOURCE_ERRORS as exc:
                    run.error(f"{channel} of {seed.label}", exc)
                    continue
                ids = run.add(records, "openalex", channel, seed_paper_id=seed.paper_id)
                record_relations(conn, seed.paper_id, ids, channel, ts)
                collect(records, ids, channel)
                for pid in ids:
                    direct[pid] = "cited by the seed" if channel == "references" else "cites the seed"
            if seed.record and seed.record.related:
                try:
                    records = sources.openalex.get_many(seed.record.related[:limit])
                    collect(records, run.add(records, "openalex", "related", seed_paper_id=seed.paper_id), "related")
                except SOURCE_ERRORS as exc:
                    run.error(f"related works of {seed.label}", exc)
        for query in build_queries(seed.title, seed.abstract, cfg.keyword_queries, embedder):
            for source in (sources.openalex, sources.arxiv):
                try:
                    records = source.search(query, min(limit, 25))
                except SOURCE_ERRORS as exc:
                    run.error(f"{source.name} '{query}'", exc)
                    continue
                ids = run.add(records, source.name, "keywords", query, seed_paper_id=seed.paper_id)
                collect(records, ids, "keywords")
                for pid in ids:
                    keyword_by_paper.setdefault(pid, query)

    neighbour_of: dict[str, str] = {}
    if not any(s.record and s.record.openalex_id for s in seeds) and channel_hits:
        # Text-only seed: no citation graph. Use the closest keyword hits as stand-in seeds and
        # follow their citations too (pseudo-relevance feedback); they add coupling, not text.
        first = [pid for pid in channel_hits if pid not in seed_ids]
        _, _, pre = _score(conn, seeds, first, channel_hits, cand_refs, direct, keyword_by_paper, {}, embedder)
        ranked = sorted(first, key=lambda pid: -pre[pid]["text"])
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, title, openalex_id FROM papers WHERE id IN ({','.join('?' * len(ranked))})", ranked)}
        neighbours = [pid for pid in ranked if rows[pid]["openalex_id"]][:NEIGHBOURS]
        for npid in neighbours:
            label = (rows[npid]["title"] or "")[:60]
            seeds.append(Seed(label=label, references=cand_refs.get(npid, set()), stand_in=True))
            for channel, fetch in (("references", sources.openalex.references), ("cited_by", sources.openalex.citing)):
                try:
                    records = fetch(rows[npid]["openalex_id"], max(10, limit // 2))
                except SOURCE_ERRORS as exc:
                    run.error(f"{channel} of {label}", exc)
                    continue
                ids = run.add(records, "openalex", "neighbours", seed_paper_id=npid)
                collect(records, ids, "neighbours")
                for pid in ids:
                    if pid != npid:
                        neighbour_of.setdefault(pid, label)

    candidates = [pid for pid in channel_hits if pid not in seed_ids]
    scores, reasons, components = _score(conn, seeds, candidates, channel_hits, cand_refs, direct,
                                         keyword_by_paper, neighbour_of, embedder)
    for pid in seed_ids:  # seeds are listed first, marked as such
        scores[pid], reasons[pid] = 1.0, ["seed paper"]
    return run.finish(sources.http, top_n, scores, reasons, components)


def _rescale(values: dict[str, float], floor: float) -> dict[str, float]:
    """Map to [0, 1] relative to the best candidate, from min(floor, lowest value).

    Plain min-max would turn tiny differences into 0 vs 1 when there are few candidates.
    floor=0 gives v / max (TF-IDF); embeddings use a floor near the model's "unrelated" level.
    """
    if not values:
        return {}
    lo = min(min(values.values()), floor)
    hi = max(values.values())
    return {k: max(0.0, (v - lo) / (hi - lo)) if hi > lo else 0.0 for k, v in values.items()}


def _score(
    conn: sqlite3.Connection,
    seeds: list[Seed],
    candidates: list[str],
    channel_hits: dict[str, set[str]],
    cand_refs: dict[str, set[str]],
    direct: dict[str, str],
    keyword_by_paper: dict[str, str],
    neighbour_of: dict[str, str],
    embedder: Embedder | None,
) -> tuple[dict[str, float], dict[str, list[str]], dict[str, dict[str, float]]]:
    if not candidates:
        return {}, {}, {}
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, title, abstract FROM papers WHERE id IN ({','.join('?' * len(candidates))})", candidates)}
    docs = [f"{rows[p]['title'] or ''}. {rows[p]['abstract'] or ''}" for p in candidates]
    seed_texts = [s.text for s in seeds if s.text]

    tfidf = {p: 0.0 for p in candidates}
    emb = {p: 0.0 for p in candidates}
    for text in seed_texts:
        for p, v in zip(candidates, tfidf_similarity(text, docs), strict=True):
            tfidf[p] = max(tfidf[p], v)
        if embedder is not None:
            for p, v in zip(candidates, embedding_similarity(embedder, text, docs), strict=True):
                emb[p] = max(emb[p], v)
    tfidf_n = _rescale(tfidf, 0.0)
    if embedder is None:
        text_score = tfidf_n
    else:
        emb_n = _rescale(emb, EMBEDDING_FLOOR)
        text_score = {p: 0.7 * emb_n[p] + 0.3 * tfidf_n[p] for p in candidates}

    coupling_raw: dict[str, float] = {}
    shared: dict[str, tuple[int, bool]] = {}
    for p in candidates:
        refs = cand_refs.get(p, set())
        best, best_n, via_stand_in = 0.0, 0, False
        for seed in seeds:
            if refs and seed.references:
                n = len(refs & seed.references)
                value = n / (len(refs) * len(seed.references)) ** 0.5
                if value > best:
                    best, best_n, via_stand_in = value, n, seed.stand_in
        coupling_raw[p], shared[p] = best, (best_n, via_stand_in)
    top_coupling = max(coupling_raw.values()) or 1.0

    scores, reasons, components = {}, {}, {}
    for p in candidates:
        channels = channel_hits[p]
        comp = {
            "text": round(text_score[p], 4),
            "coupling": round(coupling_raw[p] / top_coupling, 4),
            "direct": 1.0 if p in direct else 0.0,
            "channels": round(min(len(channels), 3) / 3, 4),
            "tfidf": tfidf[p],
            "embedding": emb[p],
        }
        scores[p] = round(sum(WEIGHTS[k] * comp[k] for k in WEIGHTS), 4)
        components[p] = comp
        why = []
        if p in direct:
            why.append(direct[p])
        n_shared, via_stand_in = shared[p]
        if n_shared:
            target = " with close matches" if via_stand_in else ""
            why.append(f"shares {n_shared} reference{'s' if n_shared > 1 else ''}{target}")
        if "related" in channels:
            why.append("related work (OpenAlex)")
        if p in keyword_by_paper:
            why.append(f"keywords: {keyword_by_paper[p]}")
        if p in neighbour_of:
            why.append(f"citation link to: {neighbour_of[p]}")
        why.append(f"text similarity {comp['text']:.2f}")
        reasons[p] = why
    return scores, reasons, components
