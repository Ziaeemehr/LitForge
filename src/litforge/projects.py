"""Project workspaces: a project owns membership and decisions; papers live in the shared library."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import yaml

from litforge.config import Paths
from litforge.storage.db import new_id, now, transaction

# Which of a project's papers an action covers, by the ✓/✗ marks set in the web UI. Each value is an SQL
# condition on `m`, a LEFT JOIN of paper_marks for the project.
MARK_SCOPES = {
    "all": "",
    "relevant": " AND m.mark = 'relevant'",
    "not-irrelevant": " AND (m.mark IS NULL OR m.mark != 'irrelevant')",
}


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
    (pdir / "decisions").mkdir(parents=True, exist_ok=True)
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


def delete_project(
    conn: sqlite3.Connection, paths: Paths, name: str, delete_exports: bool = False
) -> dict[str, object]:
    """Delete a project: its paper list, runs, marks and settings. Papers, PDFs and extracted text stay
    in the shared library (other projects may use them). Exports are the user's files and are kept
    unless `delete_exports`; even then only the `markdown` folder LitForge wrote is removed from a
    custom export folder (which may be, say, an Obsidian vault)."""
    project_id = get_project_id(conn, name)
    papers = conn.execute("SELECT COUNT(*) FROM project_papers WHERE project_id=?", (project_id,)).fetchone()[0]
    row = conn.execute("SELECT export_dir FROM projects WHERE id=?", (project_id,)).fetchone()
    folder = Path(row["export_dir"]).expanduser() if row["export_dir"] else paths.default_exports(name)
    legacy = paths.project_dir(name) / "exports"  # where older versions wrote exports
    if delete_exports:
        for d in (folder, legacy):
            shutil.rmtree(d / "markdown", ignore_errors=True)
        if not row["export_dir"] and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()  # the default folder only held this export
    with transaction(conn):
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))  # members, runs, marks cascade
    pdir = paths.project_dir(name)
    keep_legacy = legacy.is_dir() and any(legacy.iterdir())
    if pdir.is_dir():
        for child in pdir.iterdir():
            if child == legacy and keep_legacy:
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        if not keep_legacy:
            pdir.rmdir()
    if get_active(paths) == name:
        _state_file(paths).write_text(json.dumps({"active_project": None}) + "\n")
    kept = [str(d) for d in (folder, legacy) if (d / "markdown").is_dir()]
    return {"papers": papers, "kept_exports": ", ".join(kept) or None}


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
