"""Open review queue and append-only human decisions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel

from litforge.analysis.ask import SentenceDraft, get_sentences, validate_sentences
from litforge.analysis.ledger import get_spans, to_packet
from litforge.analysis.matrix import get_matrix, list_matrices
from litforge.analysis.runs import canonical_json
from litforge.storage.db import new_id, now, transaction


class ReviewItem(BaseModel):
    target_kind: Literal["sentence", "cell"]
    target_id: str
    project_id: str | None
    text: str
    flags: list[str]
    audit_verdict: str | None = None
    created_at: str
    question: str | None = None
    matrix_name: str | None = None
    paper_title: str | None = None
    column_label: str | None = None


def review_queue(conn: sqlite3.Connection, project_id: str | None = None) -> list[ReviewItem]:
    items = []
    ordering: dict[str, tuple[int, int]] = {}
    rows = conn.execute("SELECT id,project_id,config_json,rowid AS ordinal FROM analysis_runs WHERE task=\'ask\'" +
                        (" AND project_id=?" if project_id is not None else ""),
                        (project_id,) if project_id is not None else ())
    for row in rows:
        for s in get_sentences(conn, row["id"]):
            if s.review_status == "open" and s.needs_review:
                created = conn.execute("SELECT created_at FROM answer_sentences WHERE id=?", (s.id,)).fetchone()[0]
                ordering[s.id] = (row["ordinal"], s.idx)
                items.append(ReviewItem(target_kind="sentence", target_id=s.id, project_id=row["project_id"],
                                        text=s.text, flags=s.flags, audit_verdict=s.audit_verdict, created_at=created,
                                        question=json.loads(row["config_json"])["question"]))
    for matrix in list_matrices(conn, project_id):
        for c in matrix.cells:
            if c.review_status == "open" and c.needs_review:
                ordinal = conn.execute("SELECT rowid FROM analysis_runs WHERE id=?", (c.run_id,)).fetchone()[0]
                cell_order = conn.execute("SELECT rowid FROM matrix_cells WHERE id=?", (c.id,)).fetchone()[0]
                ordering[c.id] = (ordinal, cell_order)
                items.append(ReviewItem(target_kind="cell", target_id=c.id, project_id=matrix.project_id,
                                        text=str(c.value) if c.value is not None else "No reported value",
                                        matrix_name=matrix.name,
                                        paper_title=conn.execute("SELECT title FROM papers WHERE id=?",
                                                                 (c.paper_id,)).fetchone()[0],
                                        column_label=next(col.label for col in matrix.columns
                                                          if col.key == c.column_key),
                                        flags=c.flags, created_at=c.updated_at))
    return sorted(items, key=lambda i: (i.created_at, ordering[i.target_id]), reverse=True)


def record_review(conn: sqlite3.Connection, target_kind: Literal["sentence", "cell"], target_id: str,
                  action: Literal["accept", "reject", "edit"], new_value: Any = None, reason: str = "") -> str:
    if target_kind not in ("sentence", "cell") or action not in ("accept", "reject", "edit"):
        raise ValueError("invalid review target or action")
    table = "answer_sentences" if target_kind == "sentence" else "matrix_cells"
    with transaction(conn):
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (target_id,)).fetchone()
        if row is None:
            raise KeyError(target_id)
        if action == "edit" and (new_value is None or (target_kind == "sentence" and
                                                      (not isinstance(new_value, str) or not new_value.strip()))):
            raise ValueError("edit requires a new value")
        old = dict(row)
        if target_kind == "sentence":
            old["value"] = row["text"]
        else:
            old["value"] = json.loads(row["human_value_json"] if row["human_value_json"] is not None
                                      else row["value_json"])
        status = {"accept": "accepted", "reject": "rejected", "edit": "edited"}[action]
        if action == "edit":
            if target_kind == "sentence":
                ids = json.loads(row["evidence_ids_json"])
                packet = to_packet(get_spans(conn, ids)) if ids else None
                validated, _ = validate_sentences([SentenceDraft(text=new_value, kind=row["kind"], evidence_ids=ids)],
                                                   packet)
                sentence = validated[0]
                conn.execute("UPDATE answer_sentences SET text=?,kind=?,flags_json=?,audit_verdict=NULL,"
                             "audit_note='',audit_model=NULL,quotes_json='[]' WHERE id=?",
                             (sentence.text, sentence.kind, canonical_json(sentence.flags), target_id))
            else:
                # Validate the edit against its column before recording the override.
                matrix = get_matrix(conn, row["matrix_id"])
                column = next(c for c in matrix.columns if c.key == row["column_key"])
                from litforge.analysis.validation import number
                if column.type == "number" and number(new_value) is None:
                    raise ValueError("number column requires a number")
                if column.type == "categorical" and new_value not in (column.options or []):
                    raise ValueError("value is outside column options")
                if column.type == "boolean" and not isinstance(new_value, bool):
                    raise ValueError("boolean column requires true or false")
                if column.type == "text" and not isinstance(new_value, str):
                    raise ValueError("text column requires text")
                conn.execute("UPDATE matrix_cells SET human_value_json=? WHERE id=?",
                             (canonical_json(new_value), target_id))
        conn.execute(f"UPDATE {table} SET review_status=? WHERE id=?", (status, target_id))
        current = dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (target_id,)).fetchone())
        review_id = new_id()
        conn.execute("""INSERT INTO human_reviews(id,target_kind,target_id,action,old_json,new_json,reason,created_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (review_id, target_kind, target_id, action, canonical_json(old),
                      canonical_json(current), reason, now()))
    return review_id
