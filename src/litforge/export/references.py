"""Export primary references with decisions, sorted by title then paper id."""
from __future__ import annotations

import sqlite3
from collections.abc import Collection
from pathlib import Path
from typing import Literal

from litforge.screening.status import final_status
from litforge.writing.bib import _authors, bibtex_entry

STATUSES = ("included", "excluded", "uncertain", "unscreened")


def export_references(
    conn: sqlite3.Connection, project_id: str, fmt: Literal["ris", "bib"], *,
    status: Collection[str] = ("included",), paper_ids: Collection[str] | None = None,
) -> str:
    if fmt not in ("ris", "bib"):
        raise ValueError("format must be ris or bib")
    if any(value not in STATUSES for value in status):
        raise ValueError("status must be included, excluded, uncertain or unscreened")
    decisions = final_status(conn, project_id)
    rows = [conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
            for pid, decision in decisions.items()
            if decision.status in status and (paper_ids is None or pid in paper_ids)]
    rows.sort(key=lambda row: (row["title"] or "", row["id"]))
    records = []
    for row in rows:
        decision = decisions[row["id"]]
        note = f"LitForge: {decision.status} ({decision.stage}, {decision.source})"
        if decision.reason:
            note += f" — {decision.reason}"
        keyword = f"litforge-{decision.status}"
        if fmt == "bib":
            records.append(bibtex_entry(conn, row["id"], extra={"note": note, "keywords": keyword}))
            continue
        fields = [("TY", "JOUR"), ("TI", row["title"])]
        fields.extend(("AU", author) for author in _authors(row["authors_json"]))
        fields.extend((tag, row[column]) for tag, column in (
            ("PY", "year"), ("DO", "doi"), ("JO", "venue"), ("AB", "abstract"), ("UR", "oa_url")))
        fields.extend((("N1", note), ("KW", keyword)))
        lines = []
        for tag, value in fields:
            clean = " ".join(str(value).split()) if value is not None else ""
            if clean:
                lines.append(f"{tag}  - {clean}")
        records.append("\n".join([*lines, "ER  -"]) + "\n")
    return "\n".join(records)


def save_references(
    conn: sqlite3.Connection, project_id: str, fmt: Literal["ris", "bib"], target: Path, *,
    status: Collection[str] = ("included",),
) -> dict[str, str | int]:
    text = export_references(conn, project_id, fmt, status=status)
    count = sum(decision.status in status for decision in final_status(conn, project_id).values())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return {"path": str(target.resolve()), "count": count}
