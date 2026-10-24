"""Resolve primary papers only: human marks, then S2, then S1, else unscreened.

Marks use S2 when a full-text row exists, otherwise S1. Uncertain stays uncertain;
model exclusion reasons prefer the first verified violated criterion, then reason.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel

from litforge.discovery.versions import primary_paper_ids


class PaperStatus(BaseModel):
    status: Literal["included", "excluded", "uncertain", "unscreened"]
    stage: Literal["S1", "S2", "none"]
    source: Literal["human", "model", "none"]
    reason: str


def _reason(row: sqlite3.Row) -> str:
    data = json.loads(row["result_json"])
    if row["decision"] == "exclude":
        for entry in data.get("exclusion", []):
            if entry.get("status") == "violated" and entry.get("verified") is True:
                criterion = str(entry.get("criterion") or "").strip()
                if criterion:
                    return criterion
        return str(data.get("reason") or "").strip() or "excluded"
    return str(data.get("reason") or "")


def final_status(conn: sqlite3.Connection, project_id: str) -> dict[str, PaperStatus]:
    """Human decisions win at the latest reached stage; only primary versions count."""
    marks = {r["paper_id"]: r["mark"] for r in conn.execute(
        "SELECT paper_id,mark FROM paper_marks WHERE project_id=?", (project_id,))}
    s1 = {r["paper_id"]: r for r in conn.execute(
        "SELECT * FROM screening_results WHERE project_id=?", (project_id,))}
    s2 = {r["paper_id"]: r for r in conn.execute(
        "SELECT * FROM fulltext_screening WHERE project_id=?", (project_id,))}
    out = {}
    for pid in sorted(primary_paper_ids(conn, project_id)):
        stage: Literal["S1", "S2"] = "S2" if pid in s2 else "S1"
        if pid in marks:
            included = marks[pid] == "relevant"
            out[pid] = PaperStatus(status="included" if included else "excluded", stage=stage, source="human",
                                   reason="included by reviewer" if included else "excluded by reviewer")
        elif (row := s2.get(pid) or s1.get(pid)) is not None:
            status: Literal["included", "excluded", "uncertain"] = (
                "included" if row["decision"] == "include" else
                "excluded" if row["decision"] == "exclude" else "uncertain")
            out[pid] = PaperStatus(status=status,
                                   stage=stage, source="model", reason=_reason(row))
        else:
            out[pid] = PaperStatus(status="unscreened", stage="none", source="none", reason="")
    return out
