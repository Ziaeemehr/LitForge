"""Run a whole gold benchmark (labels, retrieval, answers, matrix, screening, claims) and report it."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from litforge.analysis import ask as answers
from litforge.analysis import claim_graph, claims
from litforge.analysis.evaluation import (
    AnswerReport,
    ClaimReport,
    LabelProblem,
    MatrixReport,
    RetrievalBenchmark,
    RetrievalReport,
    ScreeningReport,
    check_benchmark,
    evaluate_answers,
    evaluate_claims,
    evaluate_matrix,
    evaluate_retrieval,
    evaluate_screening,
)
from litforge.analysis.gateway import CallPreview, ModelGateway
from litforge.analysis.matrix import create_matrix, extract_matrix, list_matrices
from litforge.analysis.matrix import plan_calls as matrix_plan
from litforge.analysis.query_translate import needs_translation, search_query
from litforge.analysis.retrieve import retrieve_for_question
from litforge.indexing.fts import SearchHit
from litforge.screening import fulltext
from litforge.screening.criteria import save_criteria

CITATION_TARGET = .99


class Stage(BaseModel):
    name: str
    seconds: float = 0
    error: str | None = None
    skipped: str | None = None


class AllReport(BaseModel):
    name: str
    profile: str | None
    k: int
    threshold: float
    labels: list[LabelProblem] = Field(default_factory=list)
    stages: list[Stage] = Field(default_factory=list)
    retrieval: RetrievalReport | None = None
    translated: RetrievalReport | None = None  # retrieval after Persian questions get English search terms
    answers: AnswerReport | None = None
    matrix: MatrixReport | None = None
    screening: ScreeningReport | None = None
    claims: ClaimReport | None = None


def matrix_name(bench: RetrievalBenchmark) -> str:
    return f"{bench.name}-gold"


def _matrix_id(conn: sqlite3.Connection, bench: RetrievalBenchmark, project_id: str) -> str | None:
    return next((m.id for m in list_matrices(conn, project_id) if m.name == matrix_name(bench)), None)


def _ensure_matrix(conn: sqlite3.Connection, bench: RetrievalBenchmark, project_id: str) -> str:
    return _matrix_id(conn, bench, project_id) or create_matrix(
        conn, project_id, matrix_name(bench), bench.matrix_question or bench.name, bench.matrix_columns).id


def plan_all(conn: sqlite3.Connection, gateway: ModelGateway, bench: RetrievalBenchmark, project_id: str,
             profile: str | None = None, force: bool = False) -> list[CallPreview]:
    """Model calls of every stage that can be planned up front; claim relations depend on the claims extracted."""
    plan = [p for q in bench.questions for p in answers.plan_calls(conn, gateway, q.question, project_id,
                                                                     profile=profile)]
    if bench.matrix_columns:
        plan += matrix_plan(conn, gateway, _ensure_matrix(conn, bench, project_id), None, profile, force)
    if bench.criteria and bench.screening:
        save_criteria(conn, project_id, bench.criteria)
        plan += fulltext.plan_calls(conn, gateway, project_id, None, profile, force)
    if bench.claims:
        plan += claims.plan_calls(conn, gateway, project_id, None, profile, force)
    return plan


def run_all(conn: sqlite3.Connection, gateway: ModelGateway, bench: RetrievalBenchmark, project_id: str, *,
            profile: str | None = None, approved: bool = False, skip_models: bool = False, k: int = 10,
            threshold: float = .05, max_pairs: int = 200, force: bool = False) -> AllReport:
    """`force` redoes cached matrix cells, screening decisions, claims and relations (after a reindex or a
    prompt change); answers are always asked again."""
    report = AllReport(name=bench.name, profile=profile, k=k, threshold=threshold,
                       labels=check_benchmark(conn, bench, project_id))

    def stage(name: str, work: Callable[[], Any], reason: str | None = None) -> Any:
        if reason:
            report.stages.append(Stage(name=name, skipped=reason))
            return None
        started = time.monotonic()
        try:
            result = work()
            report.stages.append(Stage(name=name, seconds=round(time.monotonic() - started, 1)))
            return result
        except Exception as exc:  # noqa: BLE001 - one failing stage must not hide the others
            report.stages.append(Stage(name=name, seconds=round(time.monotonic() - started, 1),
                                       error=" ".join(f"{type(exc).__name__}: {exc}".splitlines())))
            return None

    def matrix() -> MatrixReport:
        matrix_id = _ensure_matrix(conn, bench, project_id)
        extract_matrix(conn, gateway, matrix_id, None, profile, approved, force)
        return evaluate_matrix(conn, bench, matrix_id)

    def screening() -> ScreeningReport:
        assert bench.criteria is not None
        save_criteria(conn, project_id, bench.criteria)
        fulltext.screen_fulltext(conn, gateway, project_id, None, profile, approved, force)
        return evaluate_screening(conn, bench, project_id, threshold)

    def claim_graph_stage() -> ClaimReport:
        if bench.criteria:
            save_criteria(conn, project_id, bench.criteria)
        claims.extract_claims(conn, gateway, project_id, None, profile, approved, force)
        claim_graph.cluster_claims(conn, project_id)
        claim_graph.classify_relations(conn, gateway, project_id, max_pairs, profile, approved, force)
        return evaluate_claims(conn, bench, project_id)

    off = "--skip-models" if skip_models else None
    report.retrieval = stage("retrieval", lambda: evaluate_retrieval(conn, bench, k, project_id))

    def translated(conn: sqlite3.Connection, question: str, project_id: str | None = None,
                   k: int = 8) -> list[SearchHit]:
        return retrieve_for_question(conn, search_query(conn, gateway, question), project_id, k)

    if any(needs_translation(q.question) for q in bench.questions):
        report.translated = stage("retrieval with translation",
                                  lambda: evaluate_retrieval(conn, bench, k, project_id, translated), off)
    report.answers = stage("answers", lambda: evaluate_answers(conn, gateway, bench, profile, project_id, approved),
                           off)
    report.matrix = stage("matrix", matrix, off or (None if bench.matrix_columns else "no matrix columns"))
    report.screening = stage("screening", screening,
                             off or (None if bench.criteria and bench.screening else "no criteria or labels"))
    report.claims = stage("claims", claim_graph_stage, off or (None if bench.claims else "no claim labels"))
    return report


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _table(rows: list[tuple[str, str, str]]) -> list[str]:
    return ["| Metric | Value | Target |", "|---|---|---|", *[f"| {a} | {b} | {c} |" for a, b, c in rows]]


def render_report(report: AllReport, bench: RetrievalBenchmark) -> str:
    out = [f"# Benchmark {report.name}", "",
           f"{date.today().isoformat()} · model profile: {report.profile or 'default per task'} · "
           f"{len(bench.questions)} questions, {len(bench.matrix)} matrix cells, {len(bench.screening)} screening "
           f"labels, {len(bench.claims)} claims, {len(bench.relations)} relations", "", "## Label check", ""]
    out += ([f"- {p.item}: {p.paper} p.{p.page} “{p.contains}”: {p.problem}" for p in report.labels]
            or ["Every labelled quote was found on its page in the indexed text."])
    out += ["", "## Stages", "", "| Stage | Time | Result |", "|---|---|---|"]
    out += [f"| {s.name} | {s.seconds:.1f} s | {s.error or (f'skipped: {s.skipped}' if s.skipped else 'ok')} |"
            for s in report.stages]
    if r := report.retrieval:
        out += ["", "## Retrieval", "", *_table(
            [(f"Recall@{r.k}", _pct(r.recall_at_k), "measured before Q&A"),
             (f"Hit rate@{r.k}", _pct(r.hit_rate_at_k), ""), ("MRR", f"{r.mrr:.3f}", "")]
            + [(f"Recall@{r.k} ({lang})", _pct(v), "") for lang, v in r.recall_by_lang.items()])]
        missed = [f"- {q.id} ({q.lang}): {t.paper} p.{t.page}" for q in r.questions for t in q.missed]
        out += ["", f"Missed targets ({len(missed)}):", "", *missed] if missed else []
    if t := report.translated:
        out += ["", "## Retrieval with translated Persian questions", "", *_table(
            [(f"Recall@{t.k} ({lang})", _pct(v), "") for lang, v in t.recall_by_lang.items()])]
        missed = [f"- {q.id}: {m.paper} p.{m.page}" for q in t.questions if q.lang != "en" for m in q.missed]
        out += ["", f"Missed Persian targets ({len(missed)}):", "", *missed] if missed else []
    if a := report.answers:
        out += ["", "## Answers", "", *_table([
            ("Citations that resolve to retrieved evidence", _pct(a.citation_validity),
             f"≥ {CITATION_TARGET:.0%} {'✓' if a.citation_validity >= CITATION_TARGET else '✗'}"),
            ("Citations on a labelled passage", _pct(a.citation_precision), "reported"),
            ("Unsupported-sentence rate", _pct(a.unsupported_sentence_rate), "reported"),
            ("Questions answered", f"{len(a.answer_runs)}/{len(a.answer_runs) + len(a.failed)}", "all")])]
        out += ["", *[f"- {q} failed: {error}" for q, error in a.failed.items()]] if a.failed else []
    if m := report.matrix:
        out += ["", "## Matrix", "", *_table([
            ("Status accuracy", _pct(m.status_accuracy), "reported"),
            ("Value accuracy", _pct(m.value_accuracy), "reported"),
            ("Citation validity", _pct(m.citation_validity), "100%")])]
    if s := report.screening:
        run = {name: st for name, st in s.stages.items() if st.screened}
        rows = [(f"{name} false-negative rate", _pct(st.false_negative_rate),
                 f"≤ {_pct(s.threshold)} {'✓' if st.false_negative_rate <= s.threshold else '✗'}")
                for name, st in run.items()]
        rows += [(f"{name} exclusion / uncertain rate", f"{_pct(st.exclusion_rate)} / {_pct(st.uncertain_rate)}", "")
                 for name, st in run.items()]
        # An unscreened paper counts as uncertain, so a stage that never ran would look like a perfect pass.
        rows += [(f"{name} screened", f"{st.screened}/{st.papers}", "" if st.screened else "not run")
                 for name, st in s.stages.items()]
        out += ["", "## Screening", "", *_table(rows)]
    if c := report.claims:
        out += ["", "## Claims", "", *_table([
            ("Claim recall", _pct(c.claim_recall), "reported"),
            ("Relation accuracy", _pct(c.relation_accuracy), "reported"),
            ("Contradiction recall", _pct(c.contradiction_recall), "reported"),
            ("Extracted claims", str(c.extracted_claims), "")])]
        missed_claims = [m.id for m in c.claims if not m.found]
        wrong = [f"- {r.a}–{r.b}: expected {r.expected}, got {r.actual or 'no relation'}"
                 for r in c.relations if r.actual != r.expected]
        out += ["", "Claims not found: " + (", ".join(missed_claims) or "none"), "", *wrong]
    return "\n".join(out).rstrip() + "\n"
