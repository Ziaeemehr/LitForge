"""Analysis run provenance and evidence ranking."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel

from litforge.analysis.ledger import EvidenceSpan
from litforge.storage.db import new_id, now, transaction

RunStatus = Literal["running", "ok", "error", "cancelled"]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def input_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class AnalysisRun(BaseModel):
    id: str
    project_id: str | None
    task: str
    input_hash: str
    prompt_version: str
    config_json: str
    status: RunStatus
    result_json: str
    error: str | None
    started_at: str
    finished_at: str | None


def start_run(conn: sqlite3.Connection, task: str, project_id: str | None, input_payload: dict[str, Any],
              config: dict[str, Any], prompt_version: str = "") -> str:
    run_id = new_id()
    conn.execute(
        """INSERT INTO analysis_runs(id, project_id, task, input_hash, prompt_version, config_json,
           status, started_at) VALUES (?,?,?,?,?,?,'running',?)""",
        (run_id, project_id, task, input_hash(input_payload), prompt_version, canonical_json(config), now()),
    )
    return run_id


def get_run(conn: sqlite3.Connection, run_id: str) -> AnalysisRun:
    row = conn.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return AnalysisRun.model_validate(dict(row))


def finish_run(conn: sqlite3.Connection, run_id: str, status: RunStatus, result: dict[str, Any],
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE analysis_runs SET status=?, result_json=?, error=?, finished_at=? WHERE id=?",
        (status, canonical_json(result), error, now(), run_id),
    )


def link_evidence(conn: sqlite3.Connection, run_id: str, spans: Sequence[EvidenceSpan],
                  scores: Sequence[float | None], pages: Sequence[int] | None = None) -> None:
    selected_pages = pages if pages is not None else [s.page for s in spans]
    with transaction(conn):
        for rank, (span, score, page) in enumerate(zip(spans, scores, selected_pages, strict=True), 1):
            conn.execute("INSERT OR IGNORE INTO run_evidence(run_id,evidence_id,rank,score,page) VALUES (?,?,?,?,?)",
                         (run_id, span.id, rank, score, page))
