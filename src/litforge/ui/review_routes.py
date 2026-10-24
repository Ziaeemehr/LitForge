"""Review reporting routes; saving uses the project's configured export folder."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from litforge.export.markdown import default_export_dir
from litforge.export.references import save_references
from litforge.projects import get_project_id
from litforge.screening.prisma import prisma_counts, prisma_svg

if TYPE_CHECKING:
    from litforge.ui.server import App


def get(app: App, conn: sqlite3.Connection, path: str, query: dict[str, str]) -> Any:
    if path != "/api/review/prisma":
        raise LookupError("not found")
    project = query.get("project", "")
    pid = get_project_id(conn, project)
    counts = prisma_counts(conn, pid)
    return {"counts": counts.model_dump(), "svg": prisma_svg(counts, project)}


def post(app: App, conn: sqlite3.Connection, path: str, body: dict[str, Any]) -> Any:
    if path == "/api/review/references":
        project = str(body.get("project") or "")
        pid = get_project_id(conn, project)
        fmt = body.get("format")
        if fmt not in ("ris", "bib"):
            raise ValueError("format must be ris or bib")
        status = body.get("status", ["included"])
        if not isinstance(status, list) or any(not isinstance(value, str) for value in status):
            raise ValueError("status must be a list of final status values")
        target = default_export_dir(conn, app.paths, project, pid) / f"references.{fmt}"
        return save_references(conn, pid, fmt, target, status=status)
    if path != "/api/review/prisma/save":
        raise LookupError("not found")
    project = str(body.get("project") or "")
    pid = get_project_id(conn, project)
    folder = default_export_dir(conn, app.paths, project, pid)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "prisma.svg"
    target.write_text(prisma_svg(prisma_counts(conn, pid), project), encoding="utf-8")
    return {"path": str(target.resolve())}
