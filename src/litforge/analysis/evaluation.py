"""Labelled retrieval benchmarks and reproducible ranking metrics."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field, model_validator

from litforge.analysis.ask import ask
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.ledger import EvidenceSpan
from litforge.analysis.matrix import CellStatus, CellValue, get_matrix
from litforge.analysis.prompts import ASK_PROMPT_VERSION
from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.runs import finish_run, start_run
from litforge.analysis.validation import number
from litforge.indexing.fts import SearchHit


class RetrievalTarget(BaseModel):
    paper: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    contains: str | None = None


class BenchmarkQuestion(BaseModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    relevant: list[RetrievalTarget] = Field(min_length=1)


class MatrixTarget(BaseModel):
    paper: str = Field(min_length=1)
    column: str = Field(min_length=1)
    status: CellStatus
    value: str | int | float | bool | None = None


class ScreeningTarget(BaseModel):
    paper: str = Field(min_length=1)
    relevant: bool = Field(strict=True)


class RetrievalBenchmark(BaseModel):
    name: str = Field(min_length=1)
    questions: list[BenchmarkQuestion] = Field(default_factory=list)
    matrix: list[MatrixTarget] = Field(default_factory=list)
    screening: list[ScreeningTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> RetrievalBenchmark:
        ids = [q.id for q in self.questions]
        if not self.questions and not self.screening:
            raise ValueError("benchmark needs questions or screening labels")
        if len({t.paper for t in self.screening}) != len(self.screening):
            raise ValueError("screening papers must be unique")
        if len(ids) != len(set(ids)):
            raise ValueError("question IDs must be unique")
        return self


class QuestionReport(BaseModel):
    id: str
    question: str
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
                and (target.contains is None or target.contains.casefold() in row["text"].casefold()))


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
                id=question.id, question=question.question, recall=len(found) / len(ranks), hit=bool(found),
                reciprocal_rank=1 / min(found) if found else 0,
                missed=[target for target, rank in zip(question.relevant, ranks, strict=True) if rank is None],
            ))
        count = len(questions)
        report = RetrievalReport(run_id=run_id, name=bench.name, k=k, questions=questions,
                                 recall_at_k=sum(q.recall for q in questions) / count,
                                 hit_rate_at_k=sum(q.hit for q in questions) / count,
                                 mrr=sum(q.reciprocal_rank for q in questions) / count)
        finish_run(conn, run_id, "ok", report.model_dump())
        return report
    except Exception:
        finish_run(conn, run_id, "error", {}, "retrieval evaluation failed")
        raise


class AnswerReport(BaseModel):
    run_id: str
    name: str
    citation_precision: float
    unsupported_sentence_rate: float
    invalid_citation_count: int
    answer_runs: list[str]


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
            and (target.contains is None or target.contains.casefold() in span.text.casefold()))


def evaluate_answers(conn: sqlite3.Connection, gateway: ModelGateway, bench: RetrievalBenchmark,
                     profile: str | None = None, project_id: str | None = None,
                     approved: bool = False) -> AnswerReport:
    run_id = start_run(conn, "eval_answers", project_id, bench.model_dump(), {"profile": profile}, ASK_PROMPT_VERSION)
    try:
        cited = correct = unsupported = sentences = invalid = 0
        answer_runs = []
        for question in bench.questions:
            answer = ask(conn, gateway, question.question, project_id=project_id, profile=profile, approved=approved)
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
        report = AnswerReport(run_id=run_id, name=bench.name, citation_precision=correct / cited if cited else 0,
                              unsupported_sentence_rate=unsupported / sentences if sentences else 0,
                              invalid_citation_count=invalid, answer_runs=answer_runs)
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
