"""Closed action registry for the research console."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from litforge.analysis import ask, claim_graph, claims, living, snapshots
from litforge.analysis.brief import write_brief
from litforge.analysis.gateway import CallPreview, ModelGateway
from litforge.analysis.matrix import (
    MatrixColumn,
    add_column,
    create_matrix,
    export_matrix,
    extract_matrix,
    get_matrix,
    list_matrices,
)
from litforge.analysis.matrix import plan_calls as matrix_plan_calls
from litforge.analysis.runs import canonical_json
from litforge.config import Paths
from litforge.discovery.run import Sources
from litforge.export.markdown import default_export_dir
from litforge.indexing.fts import current_runs, search
from litforge.screening.fulltext import plan_calls as screening_plan_calls
from litforge.screening.fulltext import screen_fulltext

CostKind = Literal["none", "local_model", "model", "network"]
StepReference = re.compile(r"^\$step_(\d+)$")


class ActionError(ValueError):
    """A deterministic, user-facing reason a step cannot run; safe to store and show."""


def _own(context: ActionContext, kind: str, owner: str | None) -> None:
    if context.project_id is not None and owner != context.project_id:
        raise ActionError(f"this {kind} belongs to another project")


def _matrix_scope(context: ActionContext, matrix_id: str) -> None:
    _own(context, "matrix", get_matrix(context.conn, matrix_id).project_id)


class ActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterPapersArgs(ActionArgs):
    year_from: int | None = None
    year_to: int | None = None
    has_parse: bool | None = None
    has_code: bool | None = None
    text: str | None = None
    decision: Literal["include", "uncertain", "exclude", "not_screened"] | None = None
    mark: Literal["relevant", "irrelevant", "unmarked"] | None = None


class SearchLibraryArgs(ActionArgs):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=200)


class AskArgs(ActionArgs):
    question: str = Field(min_length=1)
    paper_ids: list[str] | str | None = None  # limit evidence to these papers (e.g. "$step_1")


class CreateMatrixArgs(ActionArgs):
    name: str = Field(min_length=1)
    question: str = Field(min_length=1)
    columns: list[MatrixColumn] = Field(min_length=1)


class AddColumnArgs(ActionArgs):
    matrix_id: str
    column: MatrixColumn


class ExtractMatrixArgs(ActionArgs):
    matrix_id: str
    paper_ids: list[str] | str | None = None
    profile: str | None = None


class ExtractClaimsArgs(ActionArgs):
    paper_ids: list[str] | str | None = None
    profile: str | None = None


class ClusterClaimsArgs(ActionArgs):
    threshold: float = Field(default=0.55, ge=0, le=1)


class RelateClaimsArgs(ActionArgs):
    max_pairs: int = Field(default=200, ge=1, le=1000)
    profile: str | None = None


class ScreenFulltextArgs(ActionArgs):
    paper_ids: list[str] | str | None = None
    profile: str | None = None


class ExportMatrixArgs(ActionArgs):
    matrix_id: str
    format: Literal["markdown", "csv", "json"] = "markdown"
    filename: str = "matrix.md"


class WriteBriefArgs(ActionArgs):
    answer_run_id: str
    matrix_id: str | None = None
    filename: str = "brief.md"


class SnapshotArgs(ActionArgs):
    label: str = ""


class RefreshArgs(ActionArgs):
    screen: bool = True
    extract: bool = True
    claims: bool = True


@dataclass
class ActionContext:
    conn: sqlite3.Connection
    gateway: ModelGateway
    project_id: str | None
    project_name: str | None
    paths: Paths
    sources: Sources | None = None
    cloud_approved: bool = False


@dataclass(frozen=True)
class ActionSpec:
    name: str
    args_model: type[ActionArgs]
    description: str
    cost_kind: CostKind
    writes: bool
    executor: Any


def _project_paper_ids(context: ActionContext) -> list[str]:
    if context.project_id is None:
        return [r[0] for r in context.conn.execute("SELECT id FROM papers ORDER BY id")]
    return [r[0] for r in context.conn.execute(
        "SELECT paper_id FROM project_papers WHERE project_id=? ORDER BY paper_id", (context.project_id,))]


def _filter_papers(context: ActionContext, args: FilterPapersArgs) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if context.project_id is not None:
        clauses.append("EXISTS (SELECT 1 FROM project_papers pp WHERE pp.paper_id=p.id AND pp.project_id=?)")
        params.append(context.project_id)
    if args.year_from is not None:
        clauses.append("p.year>=?")
        params.append(args.year_from)
    if args.year_to is not None:
        clauses.append("p.year<=?")
        params.append(args.year_to)
    if args.has_parse is not None:
        clauses.append(("EXISTS" if args.has_parse else "NOT EXISTS") +
                       " (SELECT 1 FROM artifacts a JOIN parse_runs r ON r.artifact_id=a.id "
                       "WHERE a.paper_id=p.id AND r.is_current=1 AND r.output_dir IS NOT NULL)")
    if args.has_code is not None:
        clauses.append(("EXISTS" if args.has_code else "NOT EXISTS") +
                       " (SELECT 1 FROM repro_links rl WHERE rl.paper_id=p.id AND rl.kind='code')")
    if args.text:
        clauses.append("instr(lower(COALESCE(p.title,'') || ' ' || COALESCE(p.abstract,'')),lower(?))>0")
        params.append(args.text)
    if args.decision is not None:
        if args.decision == "not_screened":
            clauses.append("NOT EXISTS (SELECT 1 FROM screening_results s WHERE s.paper_id=p.id " +
                           ("AND s.project_id=?" if context.project_id is not None else "") + ") AND "
                           "NOT EXISTS (SELECT 1 FROM fulltext_screening f WHERE f.paper_id=p.id " +
                           ("AND f.project_id=?" if context.project_id is not None else "") + ")")
            if context.project_id is not None:
                params.extend([context.project_id, context.project_id])
        else:
            clauses.append("(EXISTS (SELECT 1 FROM screening_results s WHERE s.paper_id=p.id AND s.decision=? " +
                           ("AND s.project_id=?" if context.project_id is not None else "") + ") OR "
                           "EXISTS (SELECT 1 FROM fulltext_screening f WHERE f.paper_id=p.id AND f.decision=? " +
                           ("AND f.project_id=?" if context.project_id is not None else "") + "))")
            params.append(args.decision)
            if context.project_id is not None:
                params.append(context.project_id)
            params.append(args.decision)
            if context.project_id is not None:
                params.append(context.project_id)
    if args.mark is not None:
        if context.project_id is None and args.mark != "unmarked":
            return {"paper_ids": [], "count": 0}
        clauses.append(("NOT EXISTS" if args.mark == "unmarked" else "EXISTS") +
                       " (SELECT 1 FROM paper_marks m WHERE m.paper_id=p.id " +
                       ("AND m.project_id=?" if context.project_id is not None else "") +
                       ("" if args.mark == "unmarked" else " AND m.mark=?") + ")")
        if context.project_id is not None:
            params.append(context.project_id)
        if args.mark != "unmarked":
            params.append(args.mark)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    ids = [r[0] for r in context.conn.execute("SELECT p.id FROM papers p" + where + " ORDER BY p.id", params)]
    return {"paper_ids": ids, "count": len(ids)}


def _search_library(context: ActionContext, args: SearchLibraryArgs) -> dict[str, Any]:
    hits = search(context.conn, args.query, context.project_id, args.limit)
    return {"paper_ids": list(dict.fromkeys(hit.paper_id for hit in hits)),
            "hits": [hit.model_dump() for hit in hits]}


def _ask(context: ActionContext, args: AskArgs) -> dict[str, Any]:
    answer = ask.ask(context.conn, context.gateway, args.question, context.project_id,
                      approved=context.cloud_approved, paper_ids=_paper_ids(args.paper_ids))
    return {"run_id": answer.run_id, "answer": answer.model_dump()}


def _create_matrix(context: ActionContext, args: CreateMatrixArgs) -> dict[str, Any]:
    matrix = create_matrix(context.conn, context.project_id, args.name, args.question, args.columns)
    return {"matrix_id": matrix.id, "matrix": matrix.model_dump()}


def _add_column(context: ActionContext, args: AddColumnArgs) -> dict[str, Any]:
    _matrix_scope(context, args.matrix_id)
    matrix = add_column(context.conn, args.matrix_id, args.column)
    return {"matrix_id": matrix.id, "matrix": matrix.model_dump()}


def _paper_ids(value: list[str] | str | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise ActionError("paper_ids reference did not resolve to a paper list")
    return value


def _extract_matrix(context: ActionContext, args: ExtractMatrixArgs) -> dict[str, Any]:
    _matrix_scope(context, args.matrix_id)
    result = extract_matrix(context.conn, context.gateway, args.matrix_id, _paper_ids(args.paper_ids), args.profile,
                            context.cloud_approved)
    return {"run_id": result.run_id, "matrix_id": result.matrix.id,
            "cells": [cell.model_dump() for cell in result.cells]}


def _extract_claims(context: ActionContext, args: ExtractClaimsArgs) -> dict[str, Any]:
    if context.project_id is None:
        raise ActionError("extract_claims requires a project")
    result = claims.extract_claims(context.conn, context.gateway, context.project_id,
                                   _paper_ids(args.paper_ids), args.profile, context.cloud_approved)
    return result.model_dump()


def _cluster_claims(context: ActionContext, args: ClusterClaimsArgs) -> dict[str, Any]:
    if context.project_id is None:
        raise ActionError("cluster_claims requires a project")
    result = claim_graph.cluster_claims(context.conn, context.project_id, args.threshold)
    return {"cluster_ids": [cluster.id for cluster in result], "count": len(result)}


def _relate_claims(context: ActionContext, args: RelateClaimsArgs) -> dict[str, Any]:
    if context.project_id is None:
        raise ActionError("relate_claims requires a project")
    result = claim_graph.classify_relations(context.conn, context.gateway, context.project_id,
                                            args.max_pairs, args.profile, context.cloud_approved)
    return result.model_dump()


def _screen_fulltext(context: ActionContext, args: ScreenFulltextArgs) -> dict[str, Any]:
    if context.project_id is None:
        raise ActionError("screen_fulltext requires a project")
    result = screen_fulltext(context.conn, context.gateway, context.project_id, profile=args.profile,
                             approved=context.cloud_approved, paper_ids=_paper_ids(args.paper_ids))
    return result.model_dump()


def _export_path(context: ActionContext, filename: str, suffixes: set[str]) -> Path:
    if context.project_name is None or context.project_id is None:
        raise ActionError("exports require a project")
    if (Path(filename).name != filename or "\\" in filename or "\x00" in filename or filename in ("", ".", "..")
            or Path(filename).suffix.lower() not in suffixes):
        raise ActionError("export filename must be a simple filename with an allowed extension")
    base = default_export_dir(context.conn, context.paths, context.project_name, context.project_id).resolve()
    target = (base / filename).resolve()
    if base not in target.parents:
        raise ActionError("export must stay inside the project export folder")
    return target


def _export_matrix(context: ActionContext, args: ExportMatrixArgs) -> dict[str, Any]:
    _matrix_scope(context, args.matrix_id)
    ext = {"markdown": {".md"}, "csv": {".csv"}, "json": {".json"}}[args.format]
    path = _export_path(context, args.filename, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_matrix(context.conn, args.matrix_id, args.format), encoding="utf-8")
    return {"path": str(path), "matrix_id": args.matrix_id}


def _write_brief(context: ActionContext, args: WriteBriefArgs) -> dict[str, Any]:
    owner = context.conn.execute("SELECT project_id FROM analysis_runs WHERE id=?", (args.answer_run_id,)).fetchone()
    _own(context, "answer", owner[0] if owner else None)
    if args.matrix_id:
        _matrix_scope(context, args.matrix_id)
    path = _export_path(context, args.filename, {".md"})
    result = write_brief(context.conn, args.answer_run_id, args.matrix_id, out=path)
    return {"path": str(result), "answer_run_id": args.answer_run_id}


def _snapshot(context: ActionContext, args: SnapshotArgs) -> dict[str, Any]:
    if context.project_id is None:
        raise ActionError("snapshot requires a project")
    return {"snapshot_id": snapshots.take_snapshot(context.conn, context.project_id, args.label)}


def _refresh(context: ActionContext, args: RefreshArgs) -> dict[str, Any]:
    if context.project_id is None or context.sources is None:
        raise ActionError("refresh requires a project and configured discovery sources")
    result = living.refresh_project(context.conn, context.gateway, context.sources, context.project_id,
                                    screen=args.screen, extract=args.extract, claims=args.claims,
                                    approved_cloud=context.cloud_approved)
    return result.model_dump()


REGISTRY: dict[str, ActionSpec] = {
    spec.name: spec for spec in (
        ActionSpec("filter_papers", FilterPapersArgs, "Select project papers by metadata and review state.",
                   "none", False, _filter_papers),
        ActionSpec("search_library", SearchLibraryArgs, "Search indexed library text.", "none", False, _search_library),
        ActionSpec("ask", AskArgs, "Answer a question with cited library evidence.", "model", True, _ask),
        ActionSpec("create_matrix", CreateMatrixArgs, "Create a typed study matrix.", "none", True, _create_matrix),
        ActionSpec("add_column", AddColumnArgs, "Add a typed column to a matrix.", "none", True, _add_column),
        ActionSpec("extract_matrix", ExtractMatrixArgs, "Extract selected matrix cells.",
                   "model", True, _extract_matrix),
        ActionSpec("extract_claims", ExtractClaimsArgs, "Extract cited claims from project papers.",
                   "model", True, _extract_claims),
        ActionSpec("cluster_claims", ClusterClaimsArgs, "Cluster project claims deterministically.",
                   "none", True, _cluster_claims),
        ActionSpec("relate_claims", RelateClaimsArgs, "Assess relations between claim pairs.",
                   "model", True, _relate_claims),
        ActionSpec("screen_fulltext", ScreenFulltextArgs, "Screen project full text against saved criteria.",
                   "model", True, _screen_fulltext),
        ActionSpec("export_matrix", ExportMatrixArgs, "Export a matrix within the project export folder.",
                   "none", True, _export_matrix),
        ActionSpec("write_brief", WriteBriefArgs, "Write a cited answer brief within the project export folder.",
                   "none", True, _write_brief),
        ActionSpec("snapshot", SnapshotArgs, "Record an immutable project snapshot.", "none", True, _snapshot),
        ActionSpec("refresh", RefreshArgs, "Refresh configured discovery sources and living review analyses.",
                   "network", True, _refresh),
    )
}


def validate_action(name: str, args: dict[str, Any]) -> tuple[ActionSpec, ActionArgs]:
    if name not in REGISTRY:
        raise ValueError(f"unknown action {name!r}; choose from {', '.join(REGISTRY)}")
    spec = REGISTRY[name]
    return spec, spec.args_model.model_validate(args)


def papers_in_scope(context: ActionContext, name: str, args: ActionArgs) -> int | None:
    if isinstance(args, FilterPapersArgs):
        return int(_filter_papers(context, args)["count"])
    if isinstance(args, SearchLibraryArgs):
        return len(_search_library(context, args)["paper_ids"])
    selected = getattr(args, "paper_ids", None)
    if isinstance(selected, list):
        return len(selected)
    if isinstance(args, ExtractMatrixArgs):
        return sum(1 for run in current_runs(context.conn, get_matrix(context.conn, args.matrix_id).project_id)
                   if selected is None or run["paper_id"] in selected)
    if isinstance(args, (ExtractClaimsArgs, ScreenFulltextArgs)):
        if selected is not None and not isinstance(selected, list):
            return None
        if context.project_id is None:
            return 0
        return len(selected) if selected is not None else len(_project_paper_ids(context))
    if name in ("ask", "relate_claims"):
        return len(_project_paper_ids(context))
    return None


def action_previews(context: ActionContext, name: str, args: ActionArgs) -> list[CallPreview]:
    gateway = context.gateway
    if isinstance(args, AskArgs):
        selected = args.paper_ids if isinstance(args.paper_ids, list) else None
        return ask.plan_calls(context.conn, gateway, args.question, context.project_id, paper_ids=selected)
    if isinstance(args, ExtractMatrixArgs):
        selected = args.paper_ids if isinstance(args.paper_ids, list) else None
        return matrix_plan_calls(context.conn, gateway, args.matrix_id, selected, args.profile)
    if isinstance(args, ExtractClaimsArgs) and context.project_id is not None:
        selected = args.paper_ids if isinstance(args.paper_ids, list) else None
        return claims.plan_calls(context.conn, gateway, context.project_id, selected, args.profile)
    if isinstance(args, RelateClaimsArgs) and context.project_id is not None:
        return claim_graph.plan_calls(context.conn, gateway, context.project_id, args.max_pairs, args.profile)
    if isinstance(args, ScreenFulltextArgs) and context.project_id is not None:
        selected = args.paper_ids if isinstance(args.paper_ids, list) else None
        return screening_plan_calls(context.conn, gateway, context.project_id, profile=args.profile,
                                    paper_ids=selected)
    if isinstance(args, RefreshArgs) and context.project_id is not None:
        previews: list[CallPreview] = []
        if args.extract:
            for matrix in list_matrices(context.conn, context.project_id):
                previews.extend(matrix_plan_calls(context.conn, gateway, matrix.id))
        if args.claims:
            previews.extend(claims.plan_calls(context.conn, gateway, context.project_id))
            previews.extend(claim_graph.plan_calls(context.conn, gateway, context.project_id))
        if args.screen:
            previews.extend(screening_plan_calls(context.conn, gateway, context.project_id))
        return previews
    return []


def resolve_references(value: Any, prior: dict[int, Any], current_step: int) -> Any:
    if isinstance(value, str):
        match = StepReference.fullmatch(value)
        if match:
            target = int(match.group(1))
            if target >= current_step:
                raise ValueError(f"forward or self reference {value} at step {current_step}")
            if target not in prior:
                raise ValueError(f"reference {value} has no successful earlier result")
            return prior[target]
        return value
    if isinstance(value, list):
        return [resolve_references(item, prior, current_step) for item in value]
    if isinstance(value, dict):
        return {key: resolve_references(item, prior, current_step) for key, item in value.items()}
    return value


def registry_schema() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    branches = []
    for name, spec in REGISTRY.items():
        schema = spec.args_model.model_json_schema()
        local_defs = schema.pop("$defs", {})
        rename = {key: f"{name}_{key}" for key in local_defs}
        for key, definition in local_defs.items():
            encoded = canonical_json(definition)
            for old, new_name in rename.items():
                encoded = encoded.replace(f"#/$defs/{old}", f"#/$defs/{new_name}")
            definitions[rename[key]] = json.loads(encoded)
        args_schema = schema
        encoded_args = canonical_json(args_schema)
        for old, new_name in rename.items():
            encoded_args = encoded_args.replace(f"#/$defs/{old}", f"#/$defs/{new_name}")
        args_schema = json.loads(encoded_args)
        branches.append({"type": "object", "additionalProperties": False,
                         "required": ["action", "args"], "properties": {
                             "action": {"const": name}, "args": args_schema}})
    return {"type": "object", "additionalProperties": False, "required": ["steps"],
            "properties": {"steps": {"type": "array", "items": {"oneOf": branches}}},
            "$defs": definitions,
            "x-action-descriptions": {name: {"description": spec.description, "cost_kind": spec.cost_kind,
                                               "writes": spec.writes} for name, spec in REGISTRY.items()}}
