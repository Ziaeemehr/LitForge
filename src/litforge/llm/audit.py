from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel, Field

from litforge.storage.db import new_id, now, transaction


class ModelCallRecord(BaseModel):
    provider: str
    model: str
    task: str
    input_hash: str
    evidence_ids: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0
    status: str
    error: str | None = None


def record_model_call(conn: sqlite3.Connection, record: ModelCallRecord) -> str:
    record_id = new_id()
    with transaction(conn):
        conn.execute(
            """INSERT INTO model_calls(
               id, provider, model, task, input_hash, evidence_ids_json, input_tokens, output_tokens,
               latency_ms, cost_usd, status, error, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record_id, record.provider, record.model, record.task, record.input_hash,
             json.dumps(record.evidence_ids, separators=(",", ":")), record.input_tokens, record.output_tokens,
             record.latency_ms, record.cost_usd, record.status, record.error, now()),
        )
    return record_id
