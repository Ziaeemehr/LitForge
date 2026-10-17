"""Append-only screening decisions shared by models and human marks."""

from __future__ import annotations

import sqlite3
from typing import Literal

from litforge.storage.db import new_id, now


def append_history(conn: sqlite3.Connection, project_id: str, paper_id: str, stage: str, decision: str,
                   source: Literal["model", "human"], criteria_hash: str, reason: str = "",
                   model: str | None = None, prompt_version: str | None = None) -> None:
    conn.execute("INSERT INTO screening_history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (new_id(), project_id, paper_id, stage, decision, source, model, prompt_version,
                  criteria_hash, reason, now()))
