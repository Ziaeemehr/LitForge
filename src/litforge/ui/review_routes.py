"""Review reporting routes; saving uses the project's configured export folder."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from litforge.analysis.gateway import GatewayError
from litforge.analysis.search_strategy import (
    build_strategy,
    cached_strategy,
    openalex_queries,
    preview_strategy,
)
from litforge.export.markdown import default_export_dir
from litforge.export.references import save_references
from litforge.llm.base import LlmError
from litforge.llm.budget import BudgetExceeded
from litforge.llm.router import RoutingError
from litforge.projects import get_project_id
from litforge.screening.prisma import prisma_counts, prisma_svg

if TYPE_CHECKING:
    from litforge.ui.server import App


def get(app: App, conn: sqlite3.Connection, path: str, query: dict[str, str]) -> Any:
    if path == "/api/review/strategy":
        return strategy_response(app, conn, query.get("project", ""),
                                 query.get("db", "pubmed,scopus,openalex").split(","), build=False)
    if path != "/api/review/prisma":
        raise LookupError("not found")
    project = query.get("project", "")
    pid = get_project_id(conn, project)
    counts = prisma_counts(conn, pid)
    return {"counts": counts.model_dump(), "svg": prisma_svg(counts, project)}


def post(app: App, conn: sqlite3.Connection, path: str, body: dict[str, Any]) -> Any:
    if path == "/api/review/strategy":
        databases = body.get("databases", ["pubmed", "scopus", "openalex"])
        if not isinstance(databases, list) or any(not isinstance(db, str) for db in databases):
            raise ValueError("databases must be a list of database names")
        return strategy_response(app, conn, str(body.get("project") or ""), databases,
                                 build=True, approved=body.get("approved") is True)
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


def strategy_response(app: App, conn: sqlite3.Connection, project: str, databases: list[str], *,
                      build: bool, approved: bool = False) -> Any:
    pid = get_project_id(conn, project)
    result = cached_strategy(conn, pid, databases)
    if result is None:
        gateway = app.make_gateway(conn)
        try:
            preview = preview_strategy(conn, gateway, pid, databases)
            if not build or preview.blocked_reason or (preview.requires_approval and not approved):
                return {"error": preview.blocked_reason or "Search strings need a model call",
                        "needs_model_call": True, "needs_approval": preview.requires_approval,
                        "preview": preview.model_dump()}, 409
            result = build_strategy(conn, gateway, pid, databases, approved=approved)
        except (RoutingError, BudgetExceeded, GatewayError, LlmError) as exc:
            return {"error": str(exc)}, 400
    return {**result.model_dump(), "openalex_queries": openalex_queries(result)}
