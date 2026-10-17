"""Labelled retrieval benchmarks and reproducible ranking metrics."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field, model_validator

from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.runs import finish_run, start_run
from litforge.indexing.fts import SearchHit


class RetrievalTarget(BaseModel):
    paper: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    contains: str | None = None


class BenchmarkQuestion(BaseModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    relevant: list[RetrievalTarget] = Field(min_length=1)


class RetrievalBenchmark(BaseModel):
    name: str = Field(min_length=1)
    questions: list[BenchmarkQuestion] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> RetrievalBenchmark:
        ids = [q.id for q in self.questions]
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
