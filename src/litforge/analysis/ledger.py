"""Immutable snapshots of retrieved chunks with parser provenance."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from litforge.discovery.versions import format_version_label
from litforge.indexing.fts import SearchHit
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.storage.db import now, transaction


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    paper_id: str
    parse_run_id: str
    chunk_id: str
    parser: str
    parser_version: str
    kind: str
    section: str | None
    page: int
    page_start: int
    page_end: int
    bbox_json: str
    text: str
    content_hash: str
    created_at: str
    title: str | None = None
    from_image: bool = False
    version: str | None = None
    version_source: str | None = None
    version_label: str | None = None

    @property
    def citation(self) -> str:
        label = f", {self.version_label}" if self.version_label else ""
        return f"{self.title or self.paper_id}{label}, p.{self.page}"


def record_spans(conn: sqlite3.Connection, hits: Sequence[SearchHit]) -> list[EvidenceSpan]:
    ids = []
    with transaction(conn):
        for hit in hits:
            row = conn.execute(
                """SELECT c.*, r.parser, r.parser_version FROM chunks c
                   JOIN parse_runs r ON r.id=c.parse_run_id WHERE c.id=?""", (hit.chunk_id,),
            ).fetchone()
            if row is None:
                raise KeyError(hit.chunk_id)
            content_hash = hashlib.sha256(row["text"].encode()).hexdigest()
            key = f"{row['parse_run_id']}|{row['id']}|{content_hash}"
            evidence_id = "ev_" + hashlib.sha256(key.encode()).hexdigest()[:16]
            if conn.execute("SELECT 1 FROM evidence_spans WHERE id=?", (evidence_id,)).fetchone() is None:
                conn.execute(
                    """INSERT INTO evidence_spans(id,paper_id,parse_run_id,chunk_id,parser,parser_version,
                       kind,section,page,page_start,page_end,bbox_json,text,content_hash,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (evidence_id, row["paper_id"], row["parse_run_id"], row["id"], row["parser"], row["parser_version"],
                     row["kind"], row["section_path"], hit.page, row["page_start"], row["page_end"],
                     row["bbox_json"], row["text"], content_hash, now()),
                )
            ids.append(evidence_id)
    return get_spans(conn, ids)


def get_spans(conn: sqlite3.Connection, ids: Sequence[str]) -> list[EvidenceSpan]:
    spans = []
    artifact_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(artifacts)")}
    has_version_metadata = {"version", "version_source", "version_label", "source_url"} <= artifact_columns
    version_columns = (
        "a.version, a.version_source, a.version_label, a.source_url" if has_version_metadata else
        "NULL AS version, NULL AS version_source, NULL AS version_label, NULL AS source_url"
    )
    for evidence_id in ids:
        row = conn.execute(
            f"""SELECT e.*, p.title, {version_columns}
                FROM evidence_spans e LEFT JOIN papers p ON p.id=e.paper_id
                LEFT JOIN artifacts a ON a.id=(SELECT artifact_id FROM parse_runs WHERE id=e.parse_run_id)
                WHERE e.id=?""", (evidence_id,),
        ).fetchone()
        if row is None:
            raise KeyError(evidence_id)
        item = dict(row)
        if item["version"] and item["version"] != "unknown":
            item["version_label"] = format_version_label(
                item["version"], item["source_url"], item["version_label"])
        else:
            item["version"] = item["version_source"] = item["version_label"] = None
        spans.append(EvidenceSpan.model_validate(item))
    return spans


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # Keep only complete words, reserving one character for the ellipsis.
    end = max((m.end() for m in re.finditer(r"\S+", text) if m.end() <= limit - 1), default=0)
    return text[:end].rstrip() + "…"


def to_packet(spans: Sequence[EvidenceSpan], sensitive: bool = False,
              max_chars_per_item: int = 2000, pages: Sequence[int] | None = None) -> EvidencePacket:
    if max_chars_per_item < 1:
        raise ValueError("max_chars_per_item must be positive")
    if pages is not None and len(pages) != len(spans):
        raise ValueError("pages must match spans in length")
    selected_pages = pages if pages is not None else [s.page for s in spans]
    return EvidencePacket(items=[EvidenceItem(id=s.id, paper_id=s.paper_id, text=_truncate(s.text, max_chars_per_item),
                                         page=page, section=s.section, kind=s.kind, from_image=s.from_image)
                                for s, page in zip(spans, selected_pages, strict=True)], sensitive=sensitive)
