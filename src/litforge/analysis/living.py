"""Incremental living reviews: replay searches, compare immutable state, export a delta."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from litforge.analysis import claim_graph
from litforge.analysis import claims as claim_analysis
from litforge.analysis.errors import run_error
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.markdown_safe import inert
from litforge.analysis.matrix import extract_matrix, list_matrices
from litforge.analysis.snapshots import (
    Signal,
    SnapshotDelta,
    diff_snapshots,
    get_snapshot,
    list_snapshots,
    significance,
    take_snapshot,
)
from litforge.config import Paths
from litforge.discovery.run import Sources, run_search
from litforge.export.markdown import default_export_dir
from litforge.screening.fulltext import screen_fulltext
from litforge.screening.run import screen_papers
from litforge.storage.db import new_id, now
from litforge.storage.recovery import process_started_at, repair_abandoned_runs


class WatchRun(BaseModel):
    id: str
    project_id: str
    before_snapshot: str
    after_snapshot: str | None = None
    delta: SnapshotDelta | None = None
    signals: list[Signal] = Field(default_factory=list)
    status: Literal["running", "ok", "partial", "error", "cancelled"]
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    report_path: str = ""


def get_watch_run(conn: sqlite3.Connection, run_id: str) -> WatchRun:
    row = conn.execute("SELECT * FROM watch_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    delta = json.loads(row["delta_json"])
    return WatchRun(**{**dict(row), "report_path": row["report_path"] or ""},
                    delta=SnapshotDelta.model_validate(delta) if delta else None,
                    signals=json.loads(row["signals_json"]))


def delta_markdown(delta: SnapshotDelta, signals: list[Signal] | None = None) -> str:
    signals = significance(delta) if signals is None else signals
    lines = ["# Living review delta", "", f"Snapshots: {delta.old_id} → {delta.new_id}", "", "## Signals", ""]
    lines.extend(f"- {inert(s.message)}" for s in signals)
    if not signals:
        lines.append("No significant signals. New-paper counts alone are not significant.")
    lines.extend(["", "## Important papers added", ""])
    for p in delta.added_papers:
        if (p.included and p.priority is not None and delta.priority_p75 is not None
                and p.priority >= delta.priority_p75):
            lines.append(f"- {inert(p.title or p.id)} ({p.year or 'year not reported'}); priority {p.priority}.")
    lines.append(f"\nPapers added: {len(delta.added_papers)}; removed: {len(delta.removed_papers)}.")
    lines.extend(["", "## Results strengthened / weakened", ""])
    lines.extend(f"- {inert(c.label)}: regrouped (similar claims were merged or split; strength not compared)."
                 if c.direction == "regrouped" else
                 f"- {inert(c.label)}: {c.direction}; support {c.support_before} → {c.support_after}; "
                 f"contradictions {c.contradiction_before} → {c.contradiction_after}." for c in delta.cluster_changes)
    lines.extend(["", "## Changed matrix cells", ""])
    for c in delta.matrix_changes:
        old = f"{c.before.status}: {c.before.value}" if c.before else "absent"
        new = f"{c.after.status}: {c.after.value}" if c.after else "absent"
        lines.append(f"- {c.matrix_id} / {c.paper_id} / {inert(c.column_key)}: {inert(old)} → {inert(new)}.")
    lines.extend(["", "## Does the conclusion need rewriting?", ""])
    if delta.answer_changes:
        lines.extend(f"- Answer may need rewriting: {inert(c.question)} — {inert(c.text)} "
                     f"(evidence: {', '.join(c.before) or 'none'} → {', '.join(c.after) or 'none'})."
                     for c in delta.answer_changes)
    elif signals:
        lines.append("Review the significant signals before reusing the conclusion [interpretation].")
    else:
        lines.append("No answer evidence changes detected; unchanged answers were not regenerated [interpretation].")
    lines.extend(["", "## Claims still without replication", ""])
    lines.extend(f"- {inert(c.text)} ({c.paper_id}); single source; "
                 + ", ".join(c.evidence_ids) + "." for c in delta.single_source_claims)
    return "\n".join(lines) + "\n"


def _queries(conn: sqlite3.Connection, project_id: str) -> list[tuple[str, list[str], int]]:
    # Same text once, union of original adapters and largest original per-source limit.
    queries: dict[str, tuple[set[str], int]] = {}
    for row in conn.execute("SELECT params_json FROM discovery_runs WHERE project_id=? AND kind='search' "
                            "ORDER BY rowid", (project_id,)):
        params = json.loads(row[0])
        for query in params.get("queries", []):
            text = str(query).strip()
            if not text:
                continue
            names, limit = queries.setdefault(text, (set(), 0))
            names.update(params.get("sources", []))
            queries[text] = names, max(limit, int(params.get("limit", 25)))
    # Similar-paper discovery stores its generated keyword searches on hits, not in params_json.
    for row in conn.execute(
            "SELECT DISTINCT h.query,h.source,r.params_json FROM discovery_hits h "
            "JOIN discovery_runs r ON r.id=h.run_id WHERE r.project_id=? "
            "AND h.query IS NOT NULL AND h.relation IN ('query','keywords') ORDER BY h.query,h.source",
            (project_id,)):
        text = row["query"].strip()
        if text:
            names, limit = queries.setdefault(text, (set(), 0))
            names.add(row["source"])
            queries[text] = names, max(limit, int(json.loads(row["params_json"]).get("limit", 25)))
    return [(q, sorted(names), limit) for q, (names, limit) in queries.items()]


def refresh_project(conn: sqlite3.Connection, gateway: ModelGateway, sources: Sources, project_id: str, *,
                    screen: bool = True, extract: bool = True, claims: bool = True,
                    progress: Callable[[str], None] | None = None, approved_cloud: bool = False) -> WatchRun:
    say = progress or (lambda _m: None)
    repair_abandoned_runs(conn)
    if conn.execute("SELECT 1 FROM watch_runs WHERE project_id=? AND status='running'", (project_id,)).fetchone():
        raise ValueError("a refresh is already running in this project")
    snapshots = list_snapshots(conn, project_id)
    before_id = snapshots[0].id if snapshots else take_snapshot(conn, project_id, "Before refresh")
    before = get_snapshot(conn, before_id)
    run_id, started = new_id(), now()
    conn.execute("INSERT INTO watch_runs(id,project_id,before_snapshot,status,started_at,"
                 "owner_pid,owner_started_at,owner_process_started_at) "
                 "VALUES (?,?,?,'running',?,?,?,?)", (run_id, project_id, before_id, started, os.getpid(), started,
                                                    process_started_at(os.getpid())))
    partial = False
    try:
        # Capture these before replay creates additional discovery_runs rows.
        was_refresh = sources.http.refresh
        try:
            sources.http.refresh = True
            for query, names, limit in _queries(conn, project_id):
                report = run_search(conn, sources, project_id, [query], names, limit, top_n=0, progress=say)
                partial |= report.status != "done"
        finally:
            sources.http.refresh = was_refresh
        previous = {p.id for p in before.data.papers}
        new_papers = sorted(r[0] for r in conn.execute(
            "SELECT paper_id FROM project_papers WHERE project_id=?", (project_id,)) if r[0] not in previous)
        say(f"{len(new_papers)} new papers since the previous snapshot")
        if screen and new_papers:
            s1 = screen_papers(conn, gateway, project_id, new_papers, progress=say, approved=approved_cloud)
            s2 = screen_fulltext(conn, gateway, project_id, paper_ids=new_papers, approved=approved_cloud)
            partial |= bool(s1.failed or s2.budget_stopped)
        if extract:
            for matrix in list_matrices(conn, project_id):
                say(f"Extracting changed cells: {matrix.name}")
                result = extract_matrix(conn, gateway, matrix.id, approved=approved_cloud)
                partial |= any(c.status == "error" for c in result.cells)
        if claims:
            old_claims = {c.id for c in claim_analysis.list_claims(conn, project_id)}
            if new_papers:
                say("Extracting claims for new papers")
                extracted = claim_analysis.extract_claims(conn, gateway, project_id, new_papers,
                                                           approved=approved_cloud)
                partial |= extracted.budget_stopped
            if new_papers or old_claims:
                claim_graph.cluster_claims(conn, project_id)
            new_claims = {c.id for c in claim_analysis.list_claims(conn, project_id)} - old_claims
            if new_claims:
                related = claim_graph.classify_relations(conn, gateway, project_id,
                    max_pairs=max(1, len(new_claims) * len(claim_analysis.list_claims(conn, project_id))),
                    claim_ids=new_claims, approved=approved_cloud)
                partial |= related.budget_stopped
        after_id = take_snapshot(conn, project_id, "After refresh")
        delta = diff_snapshots(before, get_snapshot(conn, after_id))
        signals = significance(delta)
        conn.execute("UPDATE watch_runs SET after_snapshot=?,delta_json=?,signals_json=? WHERE id=?",
                     (after_id, delta.model_dump_json(), json.dumps([s.model_dump() for s in signals]), run_id))
        project = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
        database = conn.execute("PRAGMA database_list").fetchone()[2]
        paths = Paths(Path(database).parent.parent if database else Path("data"))
        out = default_export_dir(conn, paths, project["name"], project_id) / f"delta-{run_id}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(delta_markdown(delta, signals), encoding="utf-8")
        conn.execute("UPDATE watch_runs SET status=?,finished_at=?,report_path=? WHERE id=?",
                     ("partial" if partial else "ok", now(), str(out), run_id))
        say("Delta report written")
        return get_watch_run(conn, run_id)
    except BaseException as exc:
        # Ctrl-C or shutdown is not a failure of the refresh itself.
        status, error = ("error", run_error(exc)) if isinstance(exc, Exception) else ("cancelled", "cancelled")
        conn.execute("UPDATE watch_runs SET status=?,finished_at=?,error=? WHERE id=?",
                     (status, now(), error, run_id))
        raise
