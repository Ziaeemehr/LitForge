"""Project workspaces: a project owns membership and decisions; papers live in the shared library."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from litforge.config import Paths
from litforge.storage.db import new_id, now, transaction


class ProjectNotFound(LookupError):
    pass


def create_project(
    conn: sqlite3.Connection,
    paths: Paths,
    name: str,
    topic: str | None = None,
    question: str | None = None,
) -> str:
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        raise ValueError(f"project '{name}' already exists")
    project_id = new_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO projects(id, name, topic, research_question, created_at) VALUES (?,?,?,?,?)",
            (project_id, name, topic, question, now()),
        )
    pdir = paths.project_dir(name)
    for sub in ("decisions", "exports"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    project_file = pdir / "project.yaml"
    if not project_file.exists():
        config: dict[str, object] = {
            "project": name,
            "topic": topic,
            "question": question,
            "include": [],
            "exclude": [],
            "privacy": {"allow_cloud": False},
        }
        project_file.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    return project_id


def get_project_id(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ProjectNotFound(f"project '{name}' not found; create it with `litforge project create`")
    return str(row["id"])


def add_paper(conn: sqlite3.Connection, project_id: str, paper_id: str, source: str = "import") -> bool:
    """Add membership; returns True when newly added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO project_papers(project_id, paper_id, source, added_at) VALUES (?,?,?,?)",
        (project_id, paper_id, source, now()),
    )
    return cur.rowcount > 0


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.name, p.topic, p.created_at, COUNT(pp.paper_id) AS papers
           FROM projects p LEFT JOIN project_papers pp ON pp.project_id = p.id
           GROUP BY p.id ORDER BY p.created_at"""
    ).fetchall()


def project_file(paths: Paths, name: str) -> Path:
    return paths.project_dir(name) / "project.yaml"


def _state_file(paths: Paths) -> Path:
    return paths.root / "state.json"


def get_active(paths: Paths) -> str | None:
    """Name of the active project (set by `new`/`project use`), if any."""
    try:
        return json.loads(_state_file(paths).read_text()).get("active_project")  # type: ignore[no-any-return]
    except (OSError, ValueError):
        return None


def set_active(paths: Paths, name: str) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    _state_file(paths).write_text(json.dumps({"active_project": name}) + "\n")
