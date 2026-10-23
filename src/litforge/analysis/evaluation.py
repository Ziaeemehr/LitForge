"""Labelled retrieval benchmarks and reproducible ranking metrics."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from litforge.analysis.ask import ask
from litforge.analysis.claim_graph import list_relations
from litforge.analysis.claims import claim_spans, list_claims
from litforge.analysis.gateway import GatewayError, ModelGateway
from litforge.analysis.ledger import EvidenceSpan
from litforge.analysis.matrix import CellStatus, CellValue, MatrixColumn, get_matrix
from litforge.analysis.prompts import ASK_PROMPT_VERSION
from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.runs import finish_run, start_run
from litforge.analysis.validation import number
from litforge.indexing.fts import SearchHit
from litforge.llm.base import LlmError
from litforge.screening.criteria import Criteria


def normalize_text(text: str, join_hyphens: bool = True) -> str:
    """Compare quotes across parsers: ligatures, case, line-break hyphens and whitespace do not count."""
    text = unicodedata.normalize("NFKC", text).casefold().replace("\u00ad", "")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2" if join_hyphens else r"\1-\2", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains(text: str, needle: str) -> bool:
    # A hyphen at a line end is usually a break ("learn-\ning") but can be a compound ("posterior-\nbased").
    needle = normalize_text(needle)
    return needle in normalize_text(text) or needle in normalize_text(text, join_hyphens=False)


class RetrievalTarget(BaseModel):
    paper: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    contains: str | None = None


class BenchmarkQuestion(BaseModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    relevant: list[RetrievalTarget] = Field(min_length=1)
    lang: str = "en"
    answer: str | None = None


class MatrixTarget(BaseModel):
    paper: str = Field(min_length=1)
    column: str = Field(min_length=1)
    status: CellStatus
    value: str | int | float | bool | None = None
    page: int | None = Field(default=None, ge=1)
    contains: str | None = None


class ScreeningTarget(BaseModel):
    paper: str = Field(min_length=1)
    relevant: bool = Field(strict=True)
    note: str | None = None


class ClaimTarget(BaseModel):
    id: str = Field(min_length=1)
    paper: str = Field(min_length=1)
    page: int = Field(ge=1)
    contains: str = Field(min_length=1)
    text: str = Field(min_length=1)


class RelationTarget(BaseModel):
    a: str = Field(min_length=1)
    b: str = Field(min_length=1)
    relation: Literal["supports", "contradicts", "qualifies", "replicates"]


class RetrievalBenchmark(BaseModel):
    name: str = Field(min_length=1)
    questions: list[BenchmarkQuestion] = Field(default_factory=list)
    matrix_question: str | None = None
    matrix_columns: list[MatrixColumn] = Field(default_factory=list)
    matrix: list[MatrixTarget] = Field(default_factory=list)
    criteria: Criteria | None = None
    screening: list[ScreeningTarget] = Field(default_factory=list)
    claims: list[ClaimTarget] = Field(default_factory=list)
    relations: list[RelationTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> RetrievalBenchmark:
        ids = [q.id for q in self.questions]
        if not self.questions and not self.screening:
            raise ValueError("benchmark needs questions or screening labels")
        if len({t.paper for t in self.screening}) != len(self.screening):
            raise ValueError("screening papers must be unique")
        if len(ids) != len(set(ids)):
            raise ValueError("question IDs must be unique")
        claim_ids = [c.id for c in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim IDs must be unique")
        if any(r.a not in claim_ids or r.b not in claim_ids or r.a == r.b for r in self.relations):
            raise ValueError("relations must join two different listed claims")
        keys = {c.key for c in self.matrix_columns}
        if self.matrix_columns and any(t.column not in keys for t in self.matrix):
            raise ValueError("matrix targets must name a listed matrix column")
        return self


class QuestionReport(BaseModel):
    id: str
    question: str
    lang: str = "en"
    recall: float
    hit: bool
    reciprocal_rank: float
    missed: list[RetrievalTarget]


class RetrievalReport(BaseModel):
    run_id: str
    name: str
    k: int
    questions: list[QuestionReport]
    recall_at_k: float
    hit_rate_at_k: float
    mrr: float
    recall_by_lang: dict[str, float] = Field(default_factory=dict)


class Retriever(Protocol):
    def __call__(self, conn: sqlite3.Connection, question: str, project_id: str | None = None,
                 k: int = 8) -> list[SearchHit]: ...


def load_benchmark(path: Path) -> RetrievalBenchmark:
    return RetrievalBenchmark.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _matches(conn: sqlite3.Connection, hit: SearchHit, target: RetrievalTarget) -> bool:
    row = conn.execute(
        """SELECT p.id, p.doi, p.arxiv_id, c.text, c.page_start, c.page_end
           FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=? AND p.id=?""",
        (hit.chunk_id, hit.paper_id),
    ).fetchone()
    if row is None:
        return False
    paper_matches = (target.paper == row["id"] or target.paper == row["arxiv_id"]
                     or (row["doi"] is not None and target.paper.casefold() == row["doi"].casefold()))
    return bool(paper_matches and (target.page is None or row["page_start"] <= target.page <= row["page_end"])
                and (target.contains is None or _contains(row["text"], target.contains)))


def evaluate_retrieval(conn: sqlite3.Connection, bench: RetrievalBenchmark, k: int = 10,
                       project_id: str | None = None, retriever: Retriever = retrieve_for_question) -> RetrievalReport:
    if not bench.questions:
        raise ValueError("benchmark has no retrieval questions")
    if k < 1:
        raise ValueError("k must be positive")
    run_id = start_run(conn, "eval_retrieval", project_id, bench.model_dump(), {"k": k})
    try:
        questions = []
        for question in bench.questions:
            hits = retriever(conn, question.question, project_id=project_id, k=k)[:k]
            ranks = [next((rank for rank, hit in enumerate(hits, 1) if _matches(conn, hit, target)), None)
                     for target in question.relevant]
            found = [r for r in ranks if r is not None]
            questions.append(QuestionReport(
                id=question.id, question=question.question, lang=question.lang, recall=len(found) / len(ranks),
                hit=bool(found),
                reciprocal_rank=1 / min(found) if found else 0,
                missed=[target for target, rank in zip(question.relevant, ranks, strict=True) if rank is None],
            ))
        count = len(questions)
        report = RetrievalReport(run_id=run_id, name=bench.name, k=k, questions=questions,
                                 recall_at_k=sum(q.recall for q in questions) / count,
                                 hit_rate_at_k=sum(q.hit for q in questions) / count,
                                 mrr=sum(q.reciprocal_rank for q in questions) / count,
                                 recall_by_lang=_mean_by(questions, lambda q: q.lang, lambda q: q.recall))
        finish_run(conn, run_id, "ok", report.model_dump())
        return report
    except Exception:
        finish_run(conn, run_id, "error", {}, "retrieval evaluation failed")
        raise


class AnswerReport(BaseModel):
    run_id: str
    name: str
    citation_precision: float  # share of citations that land on a labelled passage
    unsupported_sentence_rate: float
    invalid_citation_count: int
    answer_runs: list[str]
    citation_validity: float = 1  # share of citations that resolve to evidence the answer actually retrieved
    failed: dict[str, str] = Field(default_factory=dict)  # question id -> error


class MatrixReport(BaseModel):
    run_id: str
    name: str
    matrix_id: str
    status_accuracy: float
    value_accuracy: float
    citation_validity: float


def _paper_matches(conn: sqlite3.Connection, paper_id: str, identifier: str) -> bool:
    row = conn.execute("SELECT id,doi,arxiv_id FROM papers WHERE id=?", (paper_id,)).fetchone()
    return row is not None and bool(identifier in (row["id"], row["arxiv_id"]) or
                                   (row["doi"] and identifier.casefold() == row["doi"].casefold()))


def _span_matches(conn: sqlite3.Connection, span: EvidenceSpan, target: RetrievalTarget) -> bool:
    return (_paper_matches(conn, span.paper_id, target.paper)
            and (target.page is None or span.page_start <= target.page <= span.page_end)
            and (target.contains is None or _contains(span.text, target.contains)))


def evaluate_answers(conn: sqlite3.Connection, gateway: ModelGateway, bench: RetrievalBenchmark,
                     profile: str | None = None, project_id: str | None = None,
                     approved: bool = False) -> AnswerReport:
    run_id = start_run(conn, "eval_answers", project_id, bench.model_dump(), {"profile": profile}, ASK_PROMPT_VERSION)
    try:
        cited = correct = unsupported = sentences = invalid = 0
        answer_runs = []
        failed: dict[str, str] = {}
        for question in bench.questions:
            try:
                answer = ask(conn, gateway, question.question, project_id=project_id, profile=profile,
                             approved=approved)
            except (LlmError, GatewayError, ValidationError) as exc:
                # One malformed reply is a result to report, not a reason to lose the other answers.
                failed[question.id] = " ".join(f"{type(exc).__name__}: {exc}".splitlines())
                continue
            answer_runs.append(answer.run_id)
            evidence = {s.id: s for s in answer.evidence}
            invalid += len(answer.invalid_citations)
            cited += len(answer.invalid_citations)
            for sentence in answer.sentences:
                sentences += 1
                unsupported += sentence.kind == "unsupported"
                for evidence_id in sentence.evidence_ids:
                    cited += 1
                    correct += any(_span_matches(conn, evidence[evidence_id], t) for t in question.relevant)
        if not answer_runs:
            raise LlmError("every question failed: " + next(iter(failed.values()), "no questions"))
        report = AnswerReport(run_id=run_id, name=bench.name, citation_precision=correct / cited if cited else 0,
                              unsupported_sentence_rate=unsupported / sentences if sentences else 0,
                              invalid_citation_count=invalid, answer_runs=answer_runs,
                              citation_validity=1 - invalid / cited if cited else 1, failed=failed)
        finish_run(conn, run_id, "ok", report.model_dump())
        return report
    except Exception:
        finish_run(conn, run_id, "error", {}, "answer evaluation failed")
        raise


def _value_matches(actual: CellValue, expected: CellValue, column_type: str) -> bool:
    if column_type == "boolean":
        return isinstance(expected, bool) and actual is expected
    if column_type == "categorical":
        return isinstance(actual, str) and actual == expected
    if column_type == "number":
        parsed, value = number(expected), number(actual)
        return (parsed is not None and value is not None
                and abs(value - parsed) <= abs(parsed) * Decimal(".01"))
    return (isinstance(actual, str) and isinstance(expected, str)
            and expected.casefold().strip() in actual.casefold().strip())


def evaluate_matrix(conn: sqlite3.Connection, bench: RetrievalBenchmark, matrix_id: str) -> MatrixReport:
    matrix = get_matrix(conn, matrix_id)
    if not bench.matrix:
        raise ValueError("benchmark has no matrix targets")
    run_id = start_run(conn, "eval_matrix", matrix.project_id, bench.model_dump(), {"matrix_id": matrix_id})
    try:
        correct_status = correct_value = values = cited = valid = 0
        for target in bench.matrix:
            cell = next((c for c in matrix.cells if c.column_key == target.column and
                         _paper_matches(conn, c.paper_id, target.paper)), None)
            correct_status += cell is not None and cell.status == target.status
            if target.status == "reported" and "value" in target.model_fields_set:
                values += 1
                correct_value += (cell is not None and cell.status == "reported"
                                  and _value_matches(cell.value, target.value,
                                      next(c.type for c in matrix.columns if c.key == target.column)))
            if cell:
                for evidence_id in cell.evidence_ids:
                    cited += 1
                    row = conn.execute("SELECT paper_id FROM evidence_spans WHERE id=?", (evidence_id,)).fetchone()
                    valid += row is not None and row["paper_id"] == cell.paper_id
        report = MatrixReport(run_id=run_id, name=bench.name, matrix_id=matrix_id,
                              status_accuracy=correct_status / len(bench.matrix),
                              value_accuracy=correct_value / values if values else 0,
                              citation_validity=valid / cited if cited else 0)
        finish_run(conn, run_id, "ok", report.model_dump())
        return report
    except Exception:
        finish_run(conn, run_id, "error", {}, "matrix evaluation failed")
        raise


class ScreeningStageReport(BaseModel):
    papers: int
    screened: int
    false_negative_rate: float
    exclusion_rate: float
    uncertain_rate: float
    shortlist_recall: float


class ScreeningReport(BaseModel):
    run_id: str
    name: str
    threshold: float
    stages: dict[str, ScreeningStageReport]
    passed: bool


def evaluate_screening(conn: sqlite3.Connection, bench: RetrievalBenchmark, project_id: str,
                       threshold: float = .05) -> ScreeningReport:
    if not bench.screening:
        raise ValueError("benchmark has no screening labels")
    if not 0 <= threshold <= 1:
        raise ValueError("false-negative threshold must be between 0 and 1")
    run_id = start_run(conn, "eval_screening", project_id, bench.model_dump(), {"threshold": threshold})
    try:
        members = [r[0] for r in conn.execute("SELECT paper_id FROM project_papers WHERE project_id=?", (project_id,))]
        resolved: dict[str, bool] = {}
        for target in bench.screening:
            matches = [p for p in members if _paper_matches(conn, p, target.paper)]
            if len(matches) != 1:
                raise ValueError("screening target must resolve to one paper in project: " + target.paper)
            paper_id = matches[0]
            if paper_id in resolved and resolved[paper_id] != target.relevant:
                raise ValueError("conflicting screening labels for paper: " + paper_id)
            resolved[paper_id] = target.relevant
        stages = {}
        for stage, table in (("S1", "screening_results"), ("S2", "fulltext_screening")):
            excluded = uncertain = fn = screened = 0
            relevant = sum(label for label in resolved.values())
            for paper, label in resolved.items():
                row = conn.execute(f"SELECT decision FROM {table} WHERE project_id=? AND paper_id=?",
                                   (project_id, paper)).fetchone()
                decision = row[0] if row else "uncertain"
                screened += row is not None
                excluded += decision == "exclude"
                uncertain += decision == "uncertain"
                fn += label and decision == "exclude"
            stages[stage] = ScreeningStageReport(papers=len(resolved), screened=screened,
                false_negative_rate=fn / relevant if relevant else 0, exclusion_rate=excluded / len(resolved),
                uncertain_rate=uncertain / len(resolved), shortlist_recall=1 - fn / relevant if relevant else 1)
        report = ScreeningReport(run_id=run_id, name=bench.name, threshold=threshold, stages=stages,
                                 passed=all(s.false_negative_rate <= threshold for s in stages.values()))
        finish_run(conn, run_id, "ok", report.model_dump())
        return report
    except Exception:
        finish_run(conn, run_id, "error", {}, "screening evaluation failed")
        raise


def _mean_by(items: list[QuestionReport], key: Callable[[QuestionReport], str],
             value: Callable[[QuestionReport], float]) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for item in items:
        groups.setdefault(key(item), []).append(value(item))
    return {k: sum(v) / len(v) for k, v in sorted(groups.items())}


class LabelProblem(BaseModel):
    item: str
    paper: str
    page: int | None
    contains: str | None
    problem: str


def _resolve_paper(conn: sqlite3.Connection, identifier: str, project_id: str | None) -> list[str]:
    members = ([r[0] for r in conn.execute("SELECT paper_id FROM project_papers WHERE project_id=?", (project_id,))]
               if project_id else [r[0] for r in conn.execute("SELECT id FROM papers")])
    return [p for p in members if _paper_matches(conn, p, identifier)]


def check_benchmark(conn: sqlite3.Connection, bench: RetrievalBenchmark,
                    project_id: str | None = None) -> list[LabelProblem]:
    """Every labelled quote must be findable on its page in the indexed text, or no model can be scored on it."""
    items: list[tuple[str, str, int | None, str | None]] = [
        (q.id, t.paper, t.page, t.contains) for q in bench.questions for t in q.relevant]
    items += [(f"matrix:{t.paper}:{t.column}", t.paper, t.page, t.contains) for t in bench.matrix]
    items += [(f"claim:{c.id}", c.paper, c.page, c.contains) for c in bench.claims]
    items += [(f"screening:{t.paper}", t.paper, None, None) for t in bench.screening]
    problems = []
    for item, paper, page, contains in items:
        matches = _resolve_paper(conn, paper, project_id)
        if len(matches) != 1:
            problems.append(LabelProblem(item=item, paper=paper, page=page, contains=contains,
                                         problem="paper not found" if not matches else "paper is ambiguous"))
            continue
        if contains is None:
            continue
        rows = conn.execute("SELECT text, page_start, page_end FROM chunks WHERE paper_id=?",
                            (matches[0],)).fetchall()
        on_page = [r for r in rows if page is None or r["page_start"] <= page <= r["page_end"]]
        if any(_contains(r["text"], contains) for r in on_page):
            continue
        elsewhere = sorted({r["page_start"] for r in rows if _contains(r["text"], contains)})
        problems.append(LabelProblem(item=item, paper=paper, page=page, contains=contains, problem=(
            f"quote is on page {', '.join(map(str, elsewhere))}" if elsewhere else "quote not in indexed text")))
    return problems


class ClaimMatch(BaseModel):
    id: str
    found: bool
    claim_ids: list[str]


class RelationMatch(BaseModel):
    a: str
    b: str
    expected: str
    actual: str | None


class ClaimReport(BaseModel):
    run_id: str
    name: str
    claim_recall: float
    relation_accuracy: float
    contradiction_recall: float
    extracted_claims: int
    claims: list[ClaimMatch]
    relations: list[RelationMatch]


def evaluate_claims(conn: sqlite3.Connection, bench: RetrievalBenchmark, project_id: str) -> ClaimReport:
    """Gold claims count as found when an extracted claim of that paper cites the labelled quote on its page."""
    if not bench.claims:
        raise ValueError("benchmark has no claim labels")
    run_id = start_run(conn, "eval_claims", project_id, bench.model_dump(), {})
    try:
        extracted = [c for c in list_claims(conn, project_id) if c.status != "rejected"]
        spans = {c.id: claim_spans(conn, c) for c in extracted}
        matched: dict[str, list[str]] = {}
        for gold in bench.claims:
            target = RetrievalTarget(paper=gold.paper, page=gold.page, contains=gold.contains)
            matched[gold.id] = [c.id for c in extracted if _paper_matches(conn, c.paper_id, gold.paper)
                                and any(_span_matches(conn, s, target) for s in spans[c.id])]
        relations = {frozenset((r.source_claim_id, r.target_claim_id)): r.relation
                     for r in list_relations(conn, project_id) if r.status != "rejected"}
        results = []
        for gold_relation in bench.relations:
            found = [relations[frozenset((x, y))] for x in matched[gold_relation.a] for y in matched[gold_relation.b]
                     if frozenset((x, y)) in relations]
            actual = (gold_relation.relation if gold_relation.relation in found
                      else found[0] if found else None)
            results.append(RelationMatch(a=gold_relation.a, b=gold_relation.b, expected=gold_relation.relation,
                                         actual=actual))
        contradictions = [r for r in results if r.expected == "contradicts"]
        report = ClaimReport(
            run_id=run_id, name=bench.name, extracted_claims=len(extracted),
            claim_recall=sum(bool(v) for v in matched.values()) / len(bench.claims),
            relation_accuracy=(sum(r.actual == r.expected for r in results) / len(results)) if results else 0,
            contradiction_recall=(sum(r.actual == "contradicts" for r in contradictions) / len(contradictions)
                                  if contradictions else 0),
            claims=[ClaimMatch(id=k, found=bool(v), claim_ids=v) for k, v in matched.items()],
            relations=results)
        finish_run(conn, run_id, "ok", report.model_dump())
        return report
    except Exception:
        finish_run(conn, run_id, "error", {}, "claim evaluation failed")
        raise
