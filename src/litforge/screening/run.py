"""Screen the top papers of a discovery run, one model call per paper, resumable and cached."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from litforge.analysis.errors import run_error
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.runs import finish_run, start_run
from litforge.config import Config
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.observability import EventLog
from litforge.screening.criteria import criteria_hash, load_criteria
from litforge.screening.decide import Decision, decide, failed_decision
from litforge.screening.history import append_history
from litforge.screening.llm import Llm, LlmError, LlmUnavailable
from litforge.screening.prompt import PROMPT_VERSION, build_messages, fingerprint, response_schema
from litforge.storage.db import now, transaction


class ScreenReport(BaseModel):
    run_id: str
    project_id: str
    model: str
    candidates: int
    screened: int = 0
    cached: int = 0
    failed: int = 0
    counts: dict[str, int] = Field(default_factory=dict)


def preview_run(conn: sqlite3.Connection, gateway: ModelGateway, run_id: str, top: int,
                profile: str | None = None) -> dict[str, Any]:
    """Read candidate/cache state and estimate S1 calls without creating a run."""
    if top < 1:
        raise ValueError("top must be at least 1")
    run = conn.execute("SELECT project_id FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise LookupError("run not found")
    criteria = load_criteria(conn, run["project_id"])
    if criteria.is_empty():
        raise ValueError("no screening criteria: fill in the research question first")
    papers = conn.execute(
        "SELECT p.*,s.fingerprint,s.error FROM discovery_results d JOIN papers p ON p.id=d.paper_id "
        "LEFT JOIN screening_results s ON s.project_id=? AND s.paper_id=p.id WHERE d.run_id=? "
        "ORDER BY COALESCE(d.priority,d.score) DESC,p.title LIMIT ?", (run["project_id"], run_id, top)).fetchall()
    model = gateway.resolve("screen", profile)[1].model
    calls = []
    cached = 0
    for paper in papers:
        fp = fingerprint(criteria, model, paper["title"], paper["abstract"])
        if paper["fingerprint"] == fp and not paper["error"]:
            cached += 1
            continue
        packet = EvidencePacket(items=[EvidenceItem(id="s1_" + paper["id"], paper_id=paper["id"], page=1,
            text=f"{paper['title'] or ''}\n{paper['abstract'] or ''}".strip() or "No title or abstract")])
        messages = build_messages(criteria, paper["title"], paper["abstract"], paper["year"], paper["venue"])
        calls.append(gateway.preview("screen", packet, messages, profile=profile, schema=response_schema(criteria)))
    return {"candidates": len(papers), "cached": cached, "number_of_calls": len(calls),
            "models": [model], "profiles": sorted({c.profile for c in calls}),
            "remote": any(c.remote for c in calls), "requires_approval": any(c.requires_approval for c in calls),
            "max_cost_usd": None if any(c.max_cost_usd is None for c in calls) else
                            sum(c.max_cost_usd or 0 for c in calls),
            "blocked_reasons": [c.blocked_reason for c in calls if c.blocked_reason],
            "calls": [c.model_dump() for c in calls]}


def screen_run(conn: sqlite3.Connection, llm: Llm | None, run_id: str, top: int,
               progress: Callable[[str], None] | None = None, log: EventLog | None = None,
               gateway: ModelGateway | None = None, profile: str | None = None,
               approved: bool = False) -> ScreenReport:
    """Screen the run's `top` papers by priority. Unchanged results (same prompt, model, criteria and text)
    are kept; each paper is committed on its own, so an interrupted run resumes where it stopped."""
    if top < 1:
        raise ValueError("top must be at least 1")
    run = conn.execute("SELECT project_id FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise LookupError("run not found")
    project_id = run["project_id"]
    if load_criteria(conn, project_id).is_empty():
        raise ValueError("no screening criteria: fill in the research question first")
    if gateway is None:
        if llm is None:
            raise ValueError("screening requires a gateway or LLM")
        llm.check()
        config = Config()
        config.models.local.model = llm.name
        gateway = ModelGateway(conn, config, providers={"local": llm})
    papers = [r[0] for r in conn.execute(
        "SELECT d.paper_id FROM discovery_results d JOIN papers p ON p.id=d.paper_id WHERE d.run_id=? "
        "ORDER BY COALESCE(d.priority,d.score) DESC,p.title LIMIT ?", (run_id, top))]
    return screen_papers(conn, gateway, project_id, papers, progress=progress, log=log, discovery_run=run_id,
                         profile=profile, approved=approved)


def screen_papers(conn: sqlite3.Connection, gateway: ModelGateway, project_id: str, paper_ids: list[str],
                  *, progress: Callable[[str], None] | None = None, log: EventLog | None = None,
                  discovery_run: str = "", profile: str | None = None, approved: bool = False) -> ScreenReport:
    """Screen selected project papers through the supplied gateway, including newly imported papers."""
    criteria = load_criteria(conn, project_id)
    if criteria.is_empty():
        raise ValueError("no screening criteria: fill in the research question first")
    members = {r[0] for r in conn.execute("SELECT paper_id FROM project_papers WHERE project_id=?", (project_id,))}
    if set(paper_ids) - members:
        raise ValueError("selected paper is outside project")
    selected = set(paper_ids)
    rows = {r["id"]: r for r in conn.execute(
        "SELECT p.id,p.title,p.abstract,p.year,p.venue,s.fingerprint,s.error FROM papers p "
        "LEFT JOIN screening_results s ON s.project_id=? AND s.paper_id=p.id ORDER BY p.id", (project_id,))
        if r["id"] in selected}
    papers = [rows[pid] for pid in dict.fromkeys(paper_ids)]
    model = gateway.resolve("screen", profile)[1].model
    say = progress or (lambda _m: None)
    report = ScreenReport(run_id=discovery_run, project_id=project_id, model=model, candidates=len(papers))
    chash, schema, counts = criteria_hash(criteria), response_schema(criteria), Counter[str]()
    say(f"screening {len(papers)} papers with {model}")
    analysis_id = start_run(conn, "screen", project_id, {"discovery_run": discovery_run, "paper_ids": paper_ids},
                            {"model": model}, PROMPT_VERSION)
    try:
        for i, p in enumerate(papers, 1):
            fp = fingerprint(criteria, model, p["title"], p["abstract"])
            if p["fingerprint"] == fp and not p["error"]:
                report.cached += 1
                continue
            messages = build_messages(criteria, p["title"], p["abstract"], p["year"], p["venue"])
            decision: Decision | None = None
            error = ""
            for _attempt in range(2):  # one retry: small models occasionally break the JSON
                try:
                    packet = EvidencePacket(items=[EvidenceItem(id="s1_" + p["id"], paper_id=p["id"], page=1,
                        text=f"{p['title'] or ''}\n{p['abstract'] or ''}".strip() or "No title or abstract")])
                    raw = gateway.call("screen", packet, messages, schema, profile=profile, approved=approved,
                                       run_id=analysis_id,
                                       prompt_version=PROMPT_VERSION).content
                    decision = decide(raw, criteria, p["title"], p["abstract"])
                    break
                except LlmUnavailable:
                    raise
                except LlmError as exc:
                    error = str(exc)
            if decision is None:
                decision = failed_decision(criteria, error)
                report.failed += 1
                if log:
                    log.emit("screening.failed", paper=p["id"], error=error)
            else:
                report.screened += 1
            counts[decision.decision] += 1
            with transaction(conn):
                conn.execute(
                    """INSERT INTO screening_results(project_id, paper_id, decision, score, result_json, error, model,
                           prompt_version, criteria_hash, fingerprint, screened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(project_id, paper_id) DO UPDATE SET decision=excluded.decision,
                           score=excluded.score, result_json=excluded.result_json, error=excluded.error,
                           model=excluded.model, prompt_version=excluded.prompt_version,
                           criteria_hash=excluded.criteria_hash, fingerprint=excluded.fingerprint,
                           screened_at=excluded.screened_at""",
                    (project_id, p["id"], decision.decision, decision.score, decision.model_dump_json(),
                     decision.error, model, PROMPT_VERSION, chash, fp, now()))
                append_history(conn, project_id, p["id"], "S1", decision.decision, "model", chash,
                               decision.reason or decision.error or "", model, PROMPT_VERSION)
            say(f"[{i}/{len(papers)}] {decision.decision:<9} {(p['title'] or '(no title)')[:70]}")
        report.counts = dict(counts)
        if log:
            log.emit("screening.run", **report.model_dump())
        say(f"done: {report.screened} screened, {report.cached} unchanged, {report.failed} failed")
        finish_run(conn, analysis_id, "ok", report.model_dump())
        return report
    except Exception as exc:
        finish_run(conn, analysis_id, "error", {}, run_error(exc))
        raise
