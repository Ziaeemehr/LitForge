"""Match records against the library and merge them without creating duplicates.

Matching order: DOI -> arXiv id -> OpenAlex id -> normalized title (+ compatible year, no
conflicting identifiers). A title match that conflicts (different DOIs, distant years) is not
merged; it is recorded in `duplicate_candidates` for human review.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from litforge.discovery.records import WorkRecord
from litforge.metadata.extract import normalize_title
from litforge.storage.db import dumps, new_id

ID_COLUMNS = ("doi", "arxiv_id", "openalex_id")
FILL_COLUMNS = ("title", "year", "venue", "abstract", "doi", "arxiv_id", "openalex_id", "oa_url")
MIN_TITLE_MATCH_LEN = 20


def ensure_norm_titles(conn: sqlite3.Connection) -> None:
    """Backfill papers.norm_title (added in migration 0002) for rows created earlier."""
    rows = conn.execute("SELECT id, title FROM papers WHERE norm_title IS NULL AND title IS NOT NULL").fetchall()
    for row in rows:
        conn.execute("UPDATE papers SET norm_title=? WHERE id=?", (normalize_title(row["title"]), row["id"]))


def _years_compatible(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= 1


def find_match(conn: sqlite3.Connection, rec: WorkRecord) -> tuple[str | None, list[str]]:
    """Return (matching paper id or None, ids of conflicting title-only lookalikes)."""
    for column in ID_COLUMNS:
        value = getattr(rec, column)
        if value:
            row = conn.execute(f"SELECT id FROM papers WHERE {column}=?", (value,)).fetchone()
            if row:
                return str(row["id"]), []
    conflicts: list[str] = []
    norm = normalize_title(rec.title) if rec.title else ""
    if len(norm) >= MIN_TITLE_MATCH_LEN:
        for row in conn.execute("SELECT id, year, doi, arxiv_id FROM papers WHERE norm_title=?", (norm,)):
            id_conflict = any(
                getattr(rec, col) and row[col] and getattr(rec, col) != row[col] for col in ("doi", "arxiv_id")
            )
            if _years_compatible(rec.year, row["year"]) and not id_conflict:
                return str(row["id"]), []
            # Same title years apart is a different work (generic titles); flag only near-misses.
            if rec.year is None or row["year"] is None or abs(rec.year - row["year"]) <= 2:
                conflicts.append(str(row["id"]))
    return None, conflicts


def _merge_metadata(existing: str | None, rec: WorkRecord, filled: list[str]) -> str:
    meta: dict[str, Any] = json.loads(existing or "{}")
    for column in filled:
        value = getattr(rec, column)
        meta[column] = {"value": value, "source": rec.source}
    sources = set(meta.get("sources", []))
    sources.add(rec.source)
    meta["sources"] = sorted(sources)
    return dumps(meta)


def apply_record(conn: sqlite3.Connection, paper_id: str, rec: WorkRecord, ts: str) -> list[str]:
    """Fill empty fields of an existing paper from `rec`; never overwrite present values
    (except the citation count, which is refreshed). Returns the filled column names."""
    row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    updates: dict[str, Any] = {}
    for column in FILL_COLUMNS:
        value = getattr(rec, column)
        if value is not None and row[column] is None:
            updates[column] = value
    if rec.authors and row["authors_json"] is None:
        updates["authors_json"] = dumps(rec.authors)
    if "title" in updates:
        updates["norm_title"] = normalize_title(updates["title"])
    if rec.doi_is_verified and (row["doi"] == rec.doi or updates.get("doi") == rec.doi):
        updates["doi_status"] = "verified"
    elif "doi" in updates:
        updates["doi_status"] = "unverified"
    # Impact metrics change over time: refresh them whenever a registry reports them.
    for column in ("cited_by_count", "fwci", "citation_percentile", "recent_citations", "venue_id"):
        value = getattr(rec, column)
        if value is not None:
            updates[column] = value
    if rec.is_retracted is not None:
        updates["is_retracted"] = int(rec.is_retracted)
    filled = [c for c in updates if c in FILL_COLUMNS or c == "authors_json"]
    updates["metadata_json"] = _merge_metadata(row["metadata_json"], rec, [c for c in filled if c in FILL_COLUMNS])
    updates["updated_at"] = ts
    assignments = ", ".join(f"{c}=?" for c in updates)
    conn.execute(f"UPDATE papers SET {assignments} WHERE id=?", (*updates.values(), paper_id))
    return filled


def upsert_record(conn: sqlite3.Connection, rec: WorkRecord, ts: str) -> tuple[str, bool]:
    """Merge `rec` into the library. Returns (paper_id, created)."""
    match, conflicts = find_match(conn, rec)
    if match is not None:
        apply_record(conn, match, rec, ts)
        store_references(conn, match, rec)
        return match, False
    paper_id = new_id()
    conn.execute(
        """INSERT INTO papers(id, title, doi, doi_status, year, authors_json, abstract, metadata_json,
           arxiv_id, openalex_id, venue, cited_by_count, oa_url, norm_title, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            paper_id, rec.title, rec.doi,
            ("verified" if rec.doi_is_verified else "unverified") if rec.doi else None,
            rec.year, dumps(rec.authors) if rec.authors else None, rec.abstract,
            _merge_metadata(None, rec, [c for c in FILL_COLUMNS if getattr(rec, c) is not None]),
            rec.arxiv_id, rec.openalex_id, rec.venue, rec.cited_by_count, rec.oa_url,
            normalize_title(rec.title) if rec.title else None, ts, ts,
        ),
    )
    for other in conflicts:
        conn.execute(
            "INSERT OR IGNORE INTO duplicate_candidates(paper_id_a, paper_id_b, reason, created_at)"
            " VALUES (?,?,?,?)",
            (other, paper_id, "title", ts),
        )
    apply_record(conn, paper_id, rec, ts)  # sets impact metrics on the new row
    store_references(conn, paper_id, rec)
    return paper_id, True


def store_references(conn: sqlite3.Connection, paper_id: str, rec: WorkRecord) -> None:
    if rec.references:
        conn.executemany("INSERT OR IGNORE INTO paper_references(paper_id, ref_id) VALUES (?,?)",
                         [(paper_id, ref) for ref in rec.references])
