"""Cite library papers and evidence from a Writing Studio document; keep its references.bib in sync."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from litforge.analysis.ledger import get_spans, record_spans
from litforge.analysis.retrieve import retrieve_for_question
from litforge.discovery.versions import _role_for_paper
from litforge.storage.db import now
from litforge.writing.bib import citation_key, paper_for_key, write_bib
from litforge.writing.documents import Document, list_files

_CITE = re.compile(r"\\(?:[a-zA-Z]*[cC]ite[a-zA-Z]*|nocite)\*?\s*(?:\[[^\]]*\]\s*){0,2}((?:\{[^}]*\}\s*)+)")
_COMMENT = re.compile(r"(?<!\\)(?:\\\\)*%.*")  # a % after an even number of backslashes starts a comment


def cited_keys(text: str) -> list[str]:
    """Keys used by \\cite-like commands, in order of first use, ignoring comments."""
    keys: dict[str, None] = {}
    for line in text.splitlines():
        code = _COMMENT.sub(lambda m: m.group(0)[:m.group(0).index("%")], line)
        for match in _CITE.finditer(code):
            for key in ",".join(re.findall(r"\{([^}]*)\}", match.group(1))).split(","):
                if key.strip():
                    keys.setdefault(key.strip(), None)
    return list(keys)


def document_keys(doc: Document) -> list[str]:
    keys: dict[str, None] = {}
    for item in list_files(doc):
        if item["name"].endswith(".tex"):
            text = (doc.folder / item["name"]).read_text(encoding="utf-8", errors="replace")
            for key in cited_keys(text):
                keys.setdefault(key, None)
    return list(keys)


def sync_bib(conn: sqlite3.Connection, doc: Document, extra_paper_ids: list[str] | None = None) -> list[str]:
    """Write LitForge entries for every cited key that names a library paper (plus `extra_paper_ids`).
    Keys LitForge does not know are left to the user's own entries."""
    ids: dict[str, None] = {}
    for key in document_keys(doc):
        if paper_id := paper_for_key(conn, key):
            ids.setdefault(paper_id, None)
    for paper_id in extra_paper_ids or []:
        ids.setdefault(paper_id, None)
    from litforge.writing.documents import _lock

    with _lock(doc.id):
        return write_bib(conn, list(ids), doc.folder / "references.bib")


def _version(conn: sqlite3.Connection, paper_id: str) -> str | None:
    role = _role_for_paper(conn, paper_id)
    return role if role in {"preprint", "published", "accepted", "revision"} else None


def search_library(conn: sqlite3.Connection, project_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    terms = [t for t in re.split(r"\s+", query.strip().lower()) if t][:8]
    sql = """SELECT p.id, p.title, p.year, p.doi, p.authors_json, p.venue, k.key
             FROM project_papers pp JOIN papers p ON p.id=pp.paper_id
             LEFT JOIN citation_keys k ON k.paper_id=p.id WHERE pp.project_id=?"""
    params: list[Any] = [project_id]
    for term in terms:
        sql += """ AND (lower(coalesce(p.title,'')) LIKE ? ESCAPE '\\'
                   OR lower(coalesce(p.authors_json,'')) LIKE ? ESCAPE '\\' OR CAST(p.year AS TEXT)=?
                   OR lower(coalesce(p.doi,''))=? OR lower(coalesce(k.key,''))=?)"""
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like, like, term, term, term]
    sql += " ORDER BY p.year DESC, p.title LIMIT ?"
    params.append(limit)
    out = []
    for row in conn.execute(sql, params):
        authors = json.loads(row["authors_json"]) if row["authors_json"] else []
        out.append({"paper_id": row["id"], "title": row["title"], "year": row["year"], "doi": row["doi"],
                    "venue": row["venue"], "authors": authors[:3], "key": row["key"],
                    "version": _version(conn, row["id"])})
    return out


def search_evidence(conn: sqlite3.Connection, project_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    hits = retrieve_for_question(conn, query, project_id, k=limit, max_per_paper=2)
    spans = record_spans(conn, hits) if hits else []
    return [{"evidence_id": s.id, "paper_id": s.paper_id, "title": s.title, "page": s.page, "section": s.section,
             "quote": hit.snippet, "version": s.version_label or _version(conn, s.paper_id)}
            for s, hit in zip(spans, hits, strict=True)]


def cite(conn: sqlite3.Connection, doc: Document, paper_id: str, evidence_id: str | None = None) -> dict[str, Any]:
    """Give the paper its key, remember where the citation came from and add its entry to references.bib."""
    if conn.execute("SELECT 1 FROM project_papers WHERE project_id=? AND paper_id=?",
                    (doc.project_id, paper_id)).fetchone() is None:
        raise LookupError("the paper is not in this document's project")
    if evidence_id is not None:
        span = get_spans(conn, [evidence_id])[0]
        if span.paper_id != paper_id:
            raise ValueError("the evidence belongs to another paper")
    key = citation_key(conn, paper_id)
    conn.execute("INSERT INTO writing_citations(document_id, key, paper_id, evidence_id, added_at) VALUES (?,?,?,?,?)",
                 (doc.id, key, paper_id, evidence_id, now()))
    sync_bib(conn, doc, [paper_id])
    return {"key": key, "paper_id": paper_id, "evidence_id": evidence_id}


def quotes_for_key(conn: sqlite3.Connection, doc: Document, key: str) -> dict[str, Any]:
    paper_id = paper_for_key(conn, key)
    if paper_id is None:
        raise LookupError(f"'{key}' is not a library citation key")
    rows = conn.execute("""SELECT DISTINCT evidence_id FROM writing_citations
                           WHERE document_id=? AND key=? AND evidence_id IS NOT NULL ORDER BY added_at""",
                        (doc.id, key)).fetchall()
    spans = get_spans(conn, [r["evidence_id"] for r in rows])
    paper = conn.execute("SELECT title, year FROM papers WHERE id=?", (paper_id,)).fetchone()
    return {"key": key, "paper_id": paper_id, "title": paper["title"], "year": paper["year"],
            "quotes": [{"evidence_id": s.id, "page": s.page, "section": s.section, "text": s.text[:1500],
                        "version": s.version_label} for s in spans]}
