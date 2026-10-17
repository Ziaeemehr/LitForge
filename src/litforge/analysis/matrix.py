"""Incremental, paper-scoped study extraction with current cells and run history."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from litforge.analysis.ask import ReviewStatus
from litforge.analysis.errors import run_error
from litforge.analysis.figures import relevant_asset_spans
from litforge.analysis.gateway import CallPreview, GatewayError, ModelGateway
from litforge.analysis.ledger import EvidenceSpan, get_spans, record_spans, to_packet
from litforge.analysis.markdown_safe import excerpt, inert
from litforge.analysis.prompts import MATRIX_PROMPT_VERSION, matrix_messages
from litforge.analysis.runs import canonical_json, finish_run, input_hash, link_evidence, start_run
from litforge.analysis.validation import citations, image_only_numbers, missing_numbers, number
from litforge.indexing.fts import current_runs, search
from litforge.llm.base import LlmError, LlmUnavailable
from litforge.llm.budget import BudgetExceeded
from litforge.llm.evidence import EvidencePacket
from litforge.llm.router import CloudApprovalRequired, RoutingError
from litforge.storage.db import new_id, now

CellValue = str | int | float | bool | None
CellStatus = Literal["reported", "not_reported", "not_found", "not_applicable", "error"]


class MatrixColumn(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
    label: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    type: Literal["text", "number", "categorical", "boolean"]
    unit: str | None = None
    options: list[str] | None = None
    parameter: bool = False
    sensitivity_analysis: bool = False
    sensitivity_for: list[str] = Field(default_factory=list)  # parameter column keys this column tests

    @model_validator(mode="after")
    def categorical_options(self) -> MatrixColumn:
        if self.type == "categorical" and (not self.options or len(self.options) != len(set(self.options))):
            raise ValueError("categorical columns require unique options")
        if self.sensitivity_for and not self.sensitivity_analysis:
            raise ValueError("sensitivity_for requires a sensitivity-analysis column")
        return self

    @property
    def column_hash(self) -> str:
        # Only what the extraction prompt depends on; gap-detector metadata must not invalidate cached cells.
        return input_hash(self.model_dump(include={"label", "instruction", "type", "unit", "options"}))


class CellDraft(BaseModel):
    status: Literal["reported", "not_reported", "not_applicable"]
    value: CellValue = None
    unit: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class ValidatedCell(BaseModel):
    status: CellStatus
    value: CellValue = None
    unit: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class MatrixCell(ValidatedCell):
    id: str
    matrix_id: str
    paper_id: str
    column_key: str
    column_hash: str
    flags: list[str] = Field(default_factory=list)
    model: str
    provider: str
    prompt_version: str
    parse_run_id: str
    cache_key: str
    run_id: str
    review_status: ReviewStatus = "open"
    human_value: CellValue = None
    model_value: CellValue = None
    updated_at: str

    @property
    def needs_review(self) -> bool:
        return bool(self.flags or self.status in ("not_found", "error"))


class Matrix(BaseModel):
    id: str
    project_id: str | None
    name: str
    question: str
    columns: list[MatrixColumn]
    cells: list[MatrixCell] = Field(default_factory=list)
    created_at: str
    updated_at: str


class MatrixResult(BaseModel):
    run_id: str
    matrix: Matrix
    cells: list[MatrixCell]


def load_columns(path: Path) -> list[MatrixColumn]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [MatrixColumn.model_validate(c) for c in (data["columns"] if isinstance(data, dict) else data)]


def _check_columns(columns: list[MatrixColumn]) -> None:
    if not columns or len({c.key for c in columns}) != len(columns):
        raise ValueError("matrix columns must be nonempty with unique keys")


def create_matrix(conn: sqlite3.Connection, project_id: str | None, name: str, question: str,
                  columns: list[MatrixColumn]) -> Matrix:
    _check_columns(columns)
    if not name.strip() or not question.strip():
        raise ValueError("matrix name and question must be nonempty")
    if conn.execute("SELECT 1 FROM matrices WHERE project_id IS ? AND name=?", (project_id, name)).fetchone():
        raise ValueError("matrix name already exists in project")
    matrix_id = new_id()
    conn.execute("INSERT INTO matrices(id,project_id,name,question,columns_json,created_at,updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (matrix_id, project_id, name, question,
                                            canonical_json([c.model_dump() for c in columns]), now(), now()))
    return get_matrix(conn, matrix_id)


def _cell(row: sqlite3.Row) -> MatrixCell:
    model_value = json.loads(row["value_json"])
    human_value = json.loads(row["human_value_json"]) if row["human_value_json"] is not None else None
    return MatrixCell(**{**dict(row), "value": human_value if row["human_value_json"] is not None else model_value,
                         "model_value": model_value, "human_value": human_value,
                         "evidence_ids": json.loads(row["evidence_ids_json"]), "flags": json.loads(row["flags_json"])})


def get_matrix(conn: sqlite3.Connection, matrix_id: str) -> Matrix:
    row = conn.execute("SELECT * FROM matrices WHERE id=?", (matrix_id,)).fetchone()
    if row is None:
        raise KeyError(matrix_id)
    return Matrix(**{**dict(row), "columns": json.loads(row["columns_json"]),
                     "cells": [_cell(c) for c in conn.execute(
                         "SELECT * FROM matrix_cells WHERE matrix_id=? AND (? IS NULL OR paper_id IN "
                         "(SELECT paper_id FROM project_papers WHERE project_id=?)) ORDER BY paper_id,column_key",
                         (matrix_id, row["project_id"], row["project_id"]))]})


def list_matrices(conn: sqlite3.Connection, project_id: str | None = None) -> list[Matrix]:
    sql = "SELECT id FROM matrices"
    rows = conn.execute(sql + (" WHERE project_id=?" if project_id is not None else "") + " ORDER BY rowid DESC",
                        (project_id,) if project_id is not None else ())
    return [get_matrix(conn, r["id"]) for r in rows]


def add_column(conn: sqlite3.Connection, matrix_id: str, column: MatrixColumn) -> Matrix:
    matrix = get_matrix(conn, matrix_id)
    columns = [*matrix.columns, column]
    _check_columns(columns)
    conn.execute("UPDATE matrices SET columns_json=?,updated_at=? WHERE id=?",
                 (canonical_json([c.model_dump() for c in columns]), now(), matrix_id))
    return get_matrix(conn, matrix_id)


def validate_cell(draft: CellDraft, column: MatrixColumn, packet: EvidencePacket
                  ) -> tuple[ValidatedCell, list[str]]:
    ids, _ = citations(draft.evidence_ids, packet)
    cell = ValidatedCell(**{**draft.model_dump(), "evidence_ids": ids})
    flags: list[str] = []
    if draft.status != "reported":
        # Values on absence statuses are not reported observations.
        cell.value = None
        return cell, flags
    if not ids:
        cell.status, cell.value = "not_found", None
        flags.append("uncited_value")
    if column.type == "number":
        parsed = number(draft.value)
        if parsed is None:
            flags.append("type_mismatch")
        elif missing_numbers(str(parsed), ids, packet):
            flags.append("from_image" if image_only_numbers(str(parsed), ids, packet) else "number_not_in_evidence")
    elif column.type == "categorical":
        if draft.value not in (column.options or []):
            flags.append("option_mismatch")
    elif ((column.type == "boolean" and not isinstance(draft.value, bool))
          or (column.type == "text" and not isinstance(draft.value, str))):
        flags.append("type_mismatch")
    return cell, flags


class _Work(BaseModel):
    paper_id: str
    parse_run_id: str
    column: MatrixColumn
    cache_key: str
    spans: list[EvidenceSpan]
    preview: CallPreview | None
    model: str
    provider: str


def _work(conn: sqlite3.Connection, gateway: ModelGateway, matrix: Matrix, paper_ids: list[str] | None,
          profile: str | None, force: bool) -> Iterator[_Work]:
    existing = {(c.paper_id, c.column_key): c for c in matrix.cells}
    # One current parse per paper; deterministic when a paper has multiple artifacts.
    runs = {r["paper_id"]: r for r in sorted(current_runs(conn, matrix.project_id),
                                             key=lambda r: (r["created_at"], r["id"]))}
    if paper_ids is not None and set(paper_ids) - runs.keys():
        raise ValueError("selected paper is outside the matrix project or has no current parse")
    selected = {p: r for p, r in runs.items() if paper_ids is None or p in paper_ids}
    cfg = gateway.config.models.profiles.get(profile or "local", gateway.config.models.local)
    for paper, run in sorted(selected.items()):
        for column in matrix.columns:
            hits = search(conn, f"{column.label} {column.instruction} {matrix.question}",
                          limit=4, max_per_paper=4, any_terms=True, paper_ids=[paper], parse_run_id=run["id"])
            spans = [s.model_copy(update={"page": h.page}) for s, h in
                     zip(record_spans(conn, hits), hits, strict=True)]
            image_spans = relevant_asset_spans(
                conn, paper, run["id"], f"{column.label} {column.instruction} {matrix.question}")
            known_ids = {span.id for span in spans}
            spans.extend(span for span in image_spans if span.id not in known_ids)
            preview = None
            provider, model = cfg.provider, cfg.model
            if spans:
                packet = to_packet(spans)
                preview = gateway.preview("matrix_extract", packet,
                                          matrix_messages(matrix.question, column.model_dump_json(), packet, spans),
                                          profile=profile)
                provider, model = preview.provider, preview.model
            cache_key = input_hash([run["id"], column.column_hash, provider, model, MATRIX_PROMPT_VERSION])
            old = existing.get((paper, column.key))
            if not force and old and old.cache_key == cache_key:
                continue
            yield _Work(paper_id=paper, parse_run_id=run["id"], column=column, cache_key=cache_key,
                        spans=spans, preview=preview, provider=provider, model=model)


def plan_calls(conn: sqlite3.Connection, gateway: ModelGateway, matrix_id: str, paper_ids: list[str] | None = None,
               profile: str | None = None, force: bool = False) -> list[CallPreview]:
    return [w.preview for w in _work(conn, gateway, get_matrix(conn, matrix_id), paper_ids, profile, force)
            if w.preview is not None]


def _save_cell(conn: sqlite3.Connection, cell: MatrixCell) -> None:
    conn.execute("""INSERT INTO matrix_cells(id,matrix_id,paper_id,column_key,column_hash,status,value_json,unit,
                    evidence_ids_json,flags_json,note,model,provider,prompt_version,parse_run_id,cache_key,run_id,
                    review_status,human_value_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(matrix_id,paper_id,column_key) DO UPDATE SET
                    column_hash=excluded.column_hash,status=excluded.status,value_json=excluded.value_json,
                    unit=excluded.unit,evidence_ids_json=excluded.evidence_ids_json,flags_json=excluded.flags_json,
                    note=excluded.note,model=excluded.model,provider=excluded.provider,
                    prompt_version=excluded.prompt_version,parse_run_id=excluded.parse_run_id,
                    cache_key=excluded.cache_key,run_id=excluded.run_id,updated_at=excluded.updated_at,
                    review_status=excluded.review_status""",
                 (cell.id, cell.matrix_id, cell.paper_id, cell.column_key, cell.column_hash, cell.status,
                  canonical_json(cell.model_value), cell.unit, canonical_json(cell.evidence_ids),
                  canonical_json(cell.flags),
                  cell.note, cell.model, cell.provider, cell.prompt_version, cell.parse_run_id, cell.cache_key,
                  cell.run_id, cell.review_status, None, cell.updated_at))


def extract_matrix(conn: sqlite3.Connection, gateway: ModelGateway, matrix_id: str, paper_ids: list[str] | None = None,
                   profile: str | None = None, approved: bool = False, force: bool = False) -> MatrixResult:
    matrix = get_matrix(conn, matrix_id)
    run_id = start_run(conn, "matrix_extract", matrix.project_id, {"matrix_id": matrix_id, "paper_ids": paper_ids},
                       {"matrix_id": matrix_id, "profile": profile, "force": force}, MATRIX_PROMPT_VERSION)
    try:
        budget = False
        for work in _work(conn, gateway, matrix, paper_ids, profile, force):
            data = ValidatedCell(status="not_found", note="No evidence retrieved.")
            flags: list[str] = []
            if work.spans:
                link_evidence(conn, run_id, work.spans, [None] * len(work.spans))
                try:
                    if budget:
                        raise BudgetExceeded("budget")
                    packet = to_packet(work.spans)
                    reply = gateway.call("matrix_extract", packet,
                                         matrix_messages(matrix.question, work.column.model_dump_json(),
                                                         packet, work.spans), CellDraft.model_json_schema(),
                                         profile=profile, approved=approved, run_id=run_id,
                                         prompt_version=MATRIX_PROMPT_VERSION)
                    data, flags = validate_cell(CellDraft.model_validate(reply.content), work.column, packet)
                except BudgetExceeded:
                    budget = True
                    data = ValidatedCell(status="error", note="budget")
                except (CloudApprovalRequired, RoutingError, LlmUnavailable):
                    raise
                except ValidationError:
                    data = ValidatedCell(status="error", note="invalid model response")
                except (GatewayError, LlmError) as exc:
                    data = ValidatedCell(status="error", note=str(exc))
            elif budget:
                data = ValidatedCell(status="error", note="budget")
            old = next((c for c in matrix.cells if c.paper_id == work.paper_id
                        and c.column_key == work.column.key), None)
            review_status: ReviewStatus = "open"
            if old and old.human_value is not None:
                changed = (old.model_value != data.value or old.status != data.status or old.unit != data.unit
                           or set(old.evidence_ids) != set(data.evidence_ids)
                           or old.parse_run_id != work.parse_run_id or old.column_hash != work.column.column_hash)
                review_status = "open" if changed else old.review_status
                if changed or "evidence_changed_since_review" in old.flags:
                    flags.append("evidence_changed_since_review")
                    _, human_flags = validate_cell(
                        CellDraft(status="reported", value=old.human_value, evidence_ids=data.evidence_ids),
                        work.column, to_packet(work.spans))
                    flags = list(dict.fromkeys([*flags, *human_flags]))
            cell = MatrixCell(**data.model_dump(), id=old.id if old else new_id(), matrix_id=matrix_id,
                              paper_id=work.paper_id, column_key=work.column.key, column_hash=work.column.column_hash,
                              flags=flags, review_status=review_status, model=work.model, provider=work.provider,
                              prompt_version=MATRIX_PROMPT_VERSION,
                              parse_run_id=work.parse_run_id, cache_key=work.cache_key, run_id=run_id,
                              model_value=data.value, updated_at=now())
            _save_cell(conn, cell)
        current = get_matrix(conn, matrix_id)
        result = MatrixResult(run_id=run_id, matrix=current, cells=current.cells)
        finish_run(conn, run_id, "ok", result.model_dump())
        return result
    except Exception as exc:
        finish_run(conn, run_id, "error", {}, run_error(exc))
        raise


def matrix_warnings(result: MatrixResult) -> list[str]:
    warnings = []
    for column in result.matrix.columns:
        cells = [c for c in result.cells if c.column_key == column.key]
        units = {c.unit for c in cells if c.status == "reported"}
        if column.type == "number" and len(units) > 1:
            names = ", ".join(sorted(u or "unspecified" for u in units))
            warnings.append(f"{column.label}: reported units differ across rows ({names}).")
        if (cells and len({c.status for c in cells}) > 1
                and sum(c.status == "not_found" for c in cells) / len(cells) >= .5):
            warnings.append(f"{column.label}: at least 50% not_found; retrieval may be weak.")
    return warnings


def _md(value: object) -> str:
    return inert(value)



def matrix_spans(conn: sqlite3.Connection, matrix: Matrix) -> list[EvidenceSpan]:
    """Use retrieval pages from each cell's run without mutating ledger snapshots."""
    runs = {e: c.run_id for c in reversed(matrix.cells) for e in c.evidence_ids}
    spans = get_spans(conn, list(runs))
    result = []
    for span in spans:
        row = conn.execute("SELECT page FROM run_evidence WHERE run_id=? AND evidence_id=?",
                           (runs[span.id], span.id)).fetchone()
        result.append(span.model_copy(update={"page": row["page"]}) if row and row["page"] is not None else span)
    return result


def export_matrix(conn: sqlite3.Connection, matrix_id: str, fmt: str) -> str:
    matrix = get_matrix(conn, matrix_id)
    result = MatrixResult(run_id="", matrix=matrix, cells=matrix.cells)
    spans = matrix_spans(conn, matrix)
    if fmt == "json":
        return canonical_json({**matrix.model_dump(), "evidence": [s.model_dump() for s in spans],
                               "warnings": matrix_warnings(result)})
    papers = sorted({c.paper_id for c in matrix.cells})
    cells = {(c.paper_id, c.column_key): c for c in matrix.cells}
    if fmt == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["paper", *(s for c in matrix.columns for s in (c.key, c.key+"_status", c.key+"_evidence",
                                                                                c.key+"_model_value", c.key+"_flags"))])
        for paper in papers:
            row: list[CellValue] = [paper]
            for column in matrix.columns:
                cell = cells.get((paper, column.key))
                row.extend([cell.value, cell.status, ";".join(cell.evidence_ids), cell.model_value,
                            ";".join(cell.flags)] if cell else ["", "not_found", "", "", ""])
            writer.writerow(row)
        return stream.getvalue()
    if fmt not in ("markdown", "md"):
        raise ValueError("format must be markdown, csv, or json")
    lines = [f"## {inert(matrix.name)}", "", "| Paper | " + " | ".join(_md(c.label) for c in matrix.columns) + " |",
             "| --- | " + " | ".join("---" for _ in matrix.columns) + " |"]
    for paper in papers:
        title = conn.execute("SELECT title FROM papers WHERE id=?", (paper,)).fetchone()
        values = [_md(title["title"] if title and title["title"] else paper)]
        for column in matrix.columns:
            cell = cells.get((paper, column.key))
            if cell is None:
                values.append("not_found")
                continue
            value = _md(cell.value) if cell.value is not None else ""
            if "evidence_changed_since_review" in cell.flags:
                value = f"human: {value}; model: {_md(cell.model_value)}"
            citations_text = "".join(f"[^{e}]" for e in cell.evidence_ids)
            flags = ", ".join(cell.flags)
            values.append(f"{value} ({cell.status}){citations_text}" + (f" [{flags}]" if flags else ""))
        lines.append("| " + " | ".join(values) + " |")
    lines.extend(["", *[f"[^{s.id}]: {inert(s.citation)} — \"{inert(excerpt(s.text))}\"" for s in spans]])
    warnings = matrix_warnings(result)
    if warnings:
        lines.extend(["", "### Warnings", "", *["- " + inert(w) for w in warnings]])
    return "\n".join(lines) + "\n"
