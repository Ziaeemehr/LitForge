"""Chunk persistence and SQLite FTS5 (BM25) search with page-exact hits."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict

from pydantic import BaseModel

from litforge.config import Config, Paths
from litforge.indexing.chunk import chunk_document
from litforge.parsing.canonical import read_canonical
from litforge.storage.db import dumps, transaction


class IndexResult(BaseModel):
    parse_run_id: str
    paper_id: str
    outcome: str  # indexed | cached | skipped
    chunks: int = 0
    message: str | None = None


class SearchHit(BaseModel):
    chunk_id: str
    paper_id: str
    title: str | None
    section: str | None
    kind: str
    page: int  # page where the first query term occurs
    page_start: int
    page_end: int
    score: float
    snippet: str


def current_runs(conn: sqlite3.Connection, project_id: str | None) -> list[sqlite3.Row]:
    sql = """SELECT r.*, a.paper_id, a.sha256 FROM parse_runs r JOIN artifacts a ON a.id = r.artifact_id
             WHERE r.is_current = 1 AND r.output_dir IS NOT NULL"""
    if project_id is None:
        return conn.execute(sql).fetchall()
    return conn.execute(
        sql + " AND a.paper_id IN (SELECT paper_id FROM project_papers WHERE project_id=?)",
        (project_id,),
    ).fetchall()


def index_run(
    conn: sqlite3.Connection, paths: Paths, config: Config, run: sqlite3.Row, force: bool = False
) -> IndexResult:
    existing = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE parse_run_id=?", (run["id"],)
    ).fetchone()[0]
    if existing and not force:
        return IndexResult(parse_run_id=run["id"], paper_id=run["paper_id"], outcome="cached",
                           chunks=existing)
    doc = read_canonical(paths.root / run["output_dir"])
    key = f"{run['sha256']}|{run['parser']}|{run['parser_version']}|{run['config_hash']}"
    cfg = config.indexing
    drafts = chunk_document(doc, key, cfg.chunk_target_tokens, cfg.chunk_overlap_tokens,
                            cfg.index_references, cfg.index_captions)
    section_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM sections WHERE parse_run_id=? ORDER BY order_idx", (run["id"],)
        )
    ]
    flags = json.loads(run["quality_json"]).get("flags", [])
    if run["status"] != "ok":
        flags = [f"parse_status:{run['status']}", *flags]
    with transaction(conn):
        # Only the current parse of an artifact stays indexed.
        conn.execute(
            "DELETE FROM chunks WHERE parse_run_id IN (SELECT id FROM parse_runs WHERE artifact_id=?)",
            (run["artifact_id"],),
        )
        for order, d in enumerate(drafts):
            conn.execute(
                """INSERT INTO chunks(id, parse_run_id, section_id, paper_id, kind, order_idx, section_path,
                   text, token_count, page_start, page_end, bbox_json, quality_flags, hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d.id, run["id"],
                 section_ids[d.section_idx] if d.section_idx < len(section_ids) else None,
                 run["paper_id"], d.kind, order, d.section_path, d.text, d.token_count,
                 d.page_start, d.page_end, dumps([asdict(s) for s in d.spans]), dumps(flags), d.hash),
            )
    return IndexResult(parse_run_id=run["id"], paper_id=run["paper_id"], outcome="indexed",
                       chunks=len(drafts))


_PHRASE_RE = re.compile(r'"([^"]+)"|(\S+)')


def build_match(query: str) -> tuple[str, list[str]]:
    """Turn free text into a safe FTS5 query (implicit AND of quoted terms/phrases)."""
    parts: list[str] = []
    terms: list[str] = []
    for phrase, word in _PHRASE_RE.findall(query):
        if phrase:
            tokens = re.findall(r"\w+", phrase)
            if tokens:
                parts.append('"' + " ".join(tokens) + '"')
                terms.append(tokens[0])
        else:
            tokens = re.findall(r"\w+", word)
            parts += [f'"{t}"' for t in tokens]
            terms += tokens
    return " ".join(parts), terms


def _hit_page(text: str, spans: list[dict[str, int]], terms: list[str], default: int) -> int:
    lowered = text.lower()
    positions = [lowered.find(t.lower()[:max(4, len(t) - 2)]) for t in terms]
    positions = [p for p in positions if p >= 0]
    if not positions:
        return default
    first = min(positions)
    for span in spans:
        if span["start"] <= first < span["end"]:
            return span["page"]
    return default


def search(
    conn: sqlite3.Connection,
    query: str,
    project_id: str | None = None,
    limit: int = 10,
    max_per_paper: int = 3,
    raw: bool = False,
) -> list[SearchHit]:
    match, terms = (query, re.findall(r"\w+", query)) if raw else build_match(query)
    if not match:
        return []
    sql = """SELECT c.id, c.paper_id, c.kind, c.section_path, c.text, c.page_start, c.page_end,
                    c.bbox_json, p.title, bm25(chunks_fts) AS score,
                    snippet(chunks_fts, 0, '[', ']', ' … ', 24) AS snip
             FROM chunks_fts
             JOIN chunks c ON c.pk = chunks_fts.rowid
             JOIN parse_runs r ON r.id = c.parse_run_id AND r.is_current = 1
             JOIN papers p ON p.id = c.paper_id
             WHERE chunks_fts MATCH ?"""
    params: list[object] = [match]
    if project_id is not None:
        sql += " AND c.paper_id IN (SELECT paper_id FROM project_papers WHERE project_id=?)"
        params.append(project_id)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit * 5)
    hits: list[SearchHit] = []
    per_paper: dict[str, int] = {}
    for row in conn.execute(sql, params):
        if per_paper.get(row["paper_id"], 0) >= max_per_paper:
            continue
        per_paper[row["paper_id"]] = per_paper.get(row["paper_id"], 0) + 1
        hits.append(SearchHit(
            chunk_id=row["id"], paper_id=row["paper_id"], title=row["title"],
            section=row["section_path"], kind=row["kind"],
            page=_hit_page(row["text"], json.loads(row["bbox_json"]), terms, row["page_start"]),
            page_start=row["page_start"], page_end=row["page_end"], score=round(row["score"], 3),
            snippet=re.sub(r"\s+", " ", row["snip"]),
        ))
        if len(hits) >= limit:
            break
    return hits
