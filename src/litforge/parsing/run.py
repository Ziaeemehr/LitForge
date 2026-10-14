"""Parse orchestration: cache lookup, fast path, routing, optional Docling, persistence."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from litforge.config import Config, Paths
from litforge.observability import NULL_LOG, EventLog
from litforge.parsing.base import ParsedDocument, Parser
from litforge.parsing.canonical import outputs_exist, write_outputs
from litforge.parsing.docling_parser import DoclingParser, docling_available
from litforge.parsing.pymupdf_parser import PyMuPDFParser
from litforge.parsing.quality import compute_quality
from litforge.parsing.router import decide
from litforge.storage.cache import config_hash, parse_output_dir, relative_to_root
from litforge.storage.db import dumps, finish_job, new_id, now, start_job, transaction

Outcome = Literal["parsed", "cached", "failed", "skipped"]


class ParseResult(BaseModel):
    artifact_id: str
    paper_id: str | None
    outcome: Outcome
    parser: str | None = None
    status: str | None = None  # parse_runs.status of the current run
    route_reasons: list[str] = []
    message: str | None = None
    elapsed_ms: float | None = None


def artifacts_for_project(conn: sqlite3.Connection, project_id: str | None) -> list[sqlite3.Row]:
    if project_id is None:
        return conn.execute(
            "SELECT * FROM artifacts WHERE status='ok' ORDER BY created_at"
        ).fetchall()
    return conn.execute(
        """SELECT a.* FROM artifacts a JOIN project_papers pp ON pp.paper_id = a.paper_id
           WHERE pp.project_id=? AND a.status='ok' ORDER BY a.created_at""",
        (project_id,),
    ).fetchall()


def _set_current(conn: sqlite3.Connection, artifact_id: str, run_id: str) -> None:
    conn.execute("UPDATE parse_runs SET is_current = (id = ?) WHERE artifact_id = ?", (run_id, artifact_id))


def _persist(
    conn: sqlite3.Connection,
    paths: Paths,
    artifact_id: str,
    parser: Parser,
    cfg_hash: str,
    out_dir: Path,
    doc: ParsedDocument | None,
    status: str,
    route: str | None,
    reasons: list[str],
    quality: dict[str, object],
    error: str | None,
    elapsed_ms: float,
) -> str:
    run_id = new_id()
    with transaction(conn):
        # Replacing a run with the same cache key cascades to its sections/chunks/assets.
        conn.execute(
            "DELETE FROM parse_runs WHERE artifact_id=? AND parser=? AND parser_version=? AND config_hash=?",
            (artifact_id, parser.name, parser.version, cfg_hash),
        )
        conn.execute(
            """INSERT INTO parse_runs(id, artifact_id, parser, parser_version, config_hash, status, route,
               route_reasons, quality_json, output_dir, error, elapsed_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, artifact_id, parser.name, parser.version, cfg_hash, status, route, dumps(reasons),
             dumps(quality), relative_to_root(paths, out_dir) if doc else None, error, elapsed_ms, now()),
        )
        if doc is not None:
            parents: list[tuple[int, str]] = []  # (level, section_id) stack
            for idx, section in enumerate(doc.sections):
                while parents and parents[-1][0] >= section.level:
                    parents.pop()
                section_id = new_id()
                conn.execute(
                    """INSERT INTO sections(id, parse_run_id, parent_id, heading, kind, level, order_idx,
                       page_start, page_end) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (section_id, run_id, parents[-1][1] if parents else None, section.heading,
                     section.kind, section.level, idx, section.page_start, section.page_end),
                )
                if section.heading:
                    parents.append((section.level, section_id))
            for asset in doc.assets:
                conn.execute(
                    """INSERT INTO assets(id, parse_run_id, kind, label, caption, page, bbox_json, path,
                       data_json, quality) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (new_id(), run_id, asset.kind, asset.label, asset.caption, asset.page,
                     dumps(asset.bbox) if asset.bbox else None,
                     relative_to_root(paths, out_dir / asset.path) if asset.path else None,
                     dumps(asset.data) if asset.data else None, asset.quality),
                )
    return run_id


def _run_parser(
    conn: sqlite3.Connection,
    paths: Paths,
    config: Config,
    artifact: sqlite3.Row,
    parser: Parser,
    title: str | None,
    force: bool,
    log: EventLog,
) -> tuple[str, Outcome, str, list[str], float | None]:
    """Run one parser with caching. Returns (run_id, outcome, status, reasons, elapsed_ms)."""
    cfg_hash = config_hash(config.parsing.model_dump())
    out_dir = parse_output_dir(paths, artifact["sha256"], parser.name, parser.version, cfg_hash)
    cached = conn.execute(
        """SELECT id, status, route_reasons FROM parse_runs
           WHERE artifact_id=? AND parser=? AND parser_version=? AND config_hash=?""",
        (artifact["id"], parser.name, parser.version, cfg_hash),
    ).fetchone()
    if cached and not force and cached["status"] != "failed" and outputs_exist(out_dir):
        return cached["id"], "cached", cached["status"], json.loads(cached["route_reasons"]), None

    pdf_path = paths.root / artifact["pdf_path"]
    start = time.perf_counter()
    try:
        doc = parser.parse(pdf_path, out_dir, config.parsing)
    except Exception as exc:  # noqa: BLE001 - recorded as a failed run, never as empty success
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        error = f"{type(exc).__name__}: {exc}"
        run_id = _persist(conn, paths, artifact["id"], parser, cfg_hash, out_dir, None, "failed",
                          None, [error], {}, error, elapsed)
        log.emit("parse.failed", artifact_id=artifact["id"], parser=parser.name, error=error)
        return run_id, "failed", "failed", [error], elapsed
    elapsed = round((time.perf_counter() - start) * 1000, 1)
    quality = compute_quality(doc, config.parsing.router)
    decision = decide(quality, config.parsing.router)
    if parser.name != "pymupdf" and decision.route == "needs_docling":
        decision.route = "pymupdf"  # Docling output is the structural fallback; don't loop
    status = "ok" if decision.route == "pymupdf" else decision.route
    write_outputs(doc, out_dir, title)
    quality_json = {**quality.model_dump(), "flags": decision.flags, "ocr_pages": decision.ocr_pages}
    run_id = _persist(conn, paths, artifact["id"], parser, cfg_hash, out_dir, doc, status,
                      decision.route, decision.reasons, quality_json, None, elapsed)
    log.emit("parse.done", artifact_id=artifact["id"], parser=parser.name, status=status,
             reasons=decision.reasons, elapsed_ms=elapsed, pages=doc.page_count)
    return run_id, "parsed", status, decision.reasons, elapsed


def parse_artifact(
    conn: sqlite3.Connection,
    paths: Paths,
    config: Config,
    artifact: sqlite3.Row,
    force: bool = False,
    use_docling: bool = True,
    log: EventLog = NULL_LOG,
) -> ParseResult:
    title_row = conn.execute("SELECT title FROM papers WHERE id=?", (artifact["paper_id"],)).fetchone()
    title = title_row["title"] if title_row else None
    job_id = start_job(conn, "parse", artifact["id"])
    run_id, outcome, status, reasons, elapsed = _run_parser(
        conn, paths, config, artifact, PyMuPDFParser(), title, force, log
    )
    parser_name = "pymupdf"
    message = None
    current_id = run_id
    if status == "needs_docling":
        if use_docling and docling_available():
            d_run, d_outcome, d_status, d_reasons, d_elapsed = _run_parser(
                conn, paths, config, artifact, DoclingParser(), title, force, log
            )
            if d_status not in ("failed", "needs_review"):
                current_id, parser_name, status, outcome = d_run, "docling", d_status, d_outcome
                reasons = reasons + [f"docling: {r}" for r in d_reasons]
            else:
                message = f"docling fallback failed: {'; '.join(d_reasons)}"
        else:
            message = "Docling not installed; kept PyMuPDF output (install with `uv sync --extra docling`)"
    with transaction(conn):
        _set_current(conn, artifact["id"], current_id)
        finish_job(conn, job_id, "failed" if status == "failed" else "done",
                   {"outcome": outcome, "status": status, "parser": parser_name})
    return ParseResult(
        artifact_id=artifact["id"], paper_id=artifact["paper_id"], outcome=outcome, parser=parser_name,
        status=status, route_reasons=reasons, message=message, elapsed_ms=elapsed,
    )
