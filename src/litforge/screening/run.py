"""Screen the top papers of a discovery run, one model call per paper, resumable and cached."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable

from pydantic import BaseModel, Field

from litforge.observability import EventLog
from litforge.screening.criteria import criteria_hash, load_criteria
from litforge.screening.decide import Decision, decide, failed_decision
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


def screen_run(conn: sqlite3.Connection, llm: Llm, run_id: str, top: int,
               progress: Callable[[str], None] | None = None, log: EventLog | None = None) -> ScreenReport:
    """Screen the run's `top` papers by priority. Unchanged results (same prompt, model, criteria and text)
    are kept; each paper is committed on its own, so an interrupted run resumes where it stopped."""
    if top < 1:
        raise ValueError("top must be at least 1")
    run = conn.execute("SELECT project_id FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise LookupError("run not found")
    project_id = run["project_id"]
    criteria = load_criteria(conn, project_id)
    if criteria.is_empty():
        raise ValueError("no screening criteria: fill in the research question first")
    llm.check()
    papers = conn.execute(
        """SELECT p.id, p.title, p.abstract, p.year, p.venue, s.fingerprint, s.error
           FROM discovery_results d JOIN papers p ON p.id = d.paper_id
           LEFT JOIN screening_results s ON s.project_id = ? AND s.paper_id = p.id
           WHERE d.run_id = ? ORDER BY COALESCE(d.priority, d.score) DESC, p.title LIMIT ?""",
        (project_id, run_id, top)).fetchall()
    say = progress or (lambda _m: None)
    report = ScreenReport(run_id=run_id, project_id=project_id, model=llm.name, candidates=len(papers))
    chash, schema, counts = criteria_hash(criteria), response_schema(criteria), Counter[str]()
    say(f"screening {len(papers)} papers with {llm.name}")
    for i, p in enumerate(papers, 1):
        fp = fingerprint(criteria, llm.name, p["title"], p["abstract"])
        if p["fingerprint"] == fp and not p["error"]:
            report.cached += 1
            continue
        messages = build_messages(criteria, p["title"], p["abstract"], p["year"], p["venue"])
        decision: Decision | None = None
        error = ""
        for _attempt in range(2):  # one retry: small models occasionally break the JSON
            try:
                decision = decide(llm.chat_json(messages, schema), criteria, p["title"], p["abstract"])
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
                 decision.error, llm.name, PROMPT_VERSION, chash, fp, now()))
        say(f"[{i}/{len(papers)}] {decision.decision:<9} {(p['title'] or '(no title)')[:70]}")
    report.counts = dict(counts)
    if log:
        log.emit("screening.run", **report.model_dump())
    say(f"done: {report.screened} screened, {report.cached} unchanged, {report.failed} failed")
    return report
