"""A project's screening criteria, stored in projects.research_question and projects.criteria_json."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from pydantic import BaseModel, Field


class Criteria(BaseModel):
    question: str
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @classmethod
    def from_text(cls, question: str, include_text: str, exclude_text: str) -> Criteria:
        """Build from form fields: one criterion per non-empty line."""

        def lines(text: str) -> list[str]:
            return [ln.strip() for ln in text.splitlines() if ln.strip()]

        return cls(question=question.strip(), include=lines(include_text), exclude=lines(exclude_text))

    def is_empty(self) -> bool:
        return not self.question.strip()


def criteria_hash(c: Criteria) -> str:
    return hashlib.sha256(json.dumps(c.model_dump(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def load_criteria(conn: sqlite3.Connection, project_id: str) -> Criteria:
    row = conn.execute("SELECT research_question, criteria_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise LookupError("project not found")
    data = json.loads(row["criteria_json"] or "{}")
    return Criteria(question=row["research_question"] or "", include=list(data.get("include", [])),
                    exclude=list(data.get("exclude", [])))


def save_criteria(conn: sqlite3.Connection, project_id: str, c: Criteria) -> None:
    conn.execute("UPDATE projects SET research_question=?, criteria_json=? WHERE id=?",
                 (c.question, json.dumps({"include": c.include, "exclude": c.exclude}, ensure_ascii=False),
                  project_id))
