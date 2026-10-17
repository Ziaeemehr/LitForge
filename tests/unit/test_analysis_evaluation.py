from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from litforge.analysis.evaluation import RetrievalBenchmark, evaluate_retrieval, load_benchmark
from litforge.indexing.fts import SearchHit


@pytest.mark.parametrize("suffix", ["yaml", "json"])
def test_benchmark_load(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"bench.{suffix}"
    path.write_text(json.dumps({"name": "bench", "questions": [
        {"id": "q", "question": "Why?", "relevant": [{"paper": "p", "page": 2, "contains": "text"}]}]}))
    assert load_benchmark(path).questions[0].relevant[0].page == 2


@pytest.mark.parametrize("questions", [[], [{"id": "q", "question": "why", "relevant": []}],
    [{"id": "q", "question": "why", "relevant": [{"paper": "p"}]}] * 2])
def test_benchmark_validation(questions: list[dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        RetrievalBenchmark.model_validate({"name": "b", "questions": questions})


def test_metrics_match_full_chunks_and_paper_identifiers(conn: sqlite3.Connection) -> None:
    conn.execute("""INSERT INTO papers(id,title,doi,arxiv_id,created_at,updated_at)
                    VALUES ('p','Title','10.1234/ABC','1234.5678','now','now')""")
    # A different matching paper has no chunks; the first hit cannot satisfy any target.
    conn.execute("INSERT INTO papers(id,created_at,updated_at) VALUES ('other','now','now')")
    conn.execute("""INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at)
                    VALUES ('a','p','sha','f',1,'ok','now')""")
    conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,created_at)
                    VALUES ('r','a','fake','1','h','ok','now')""")
    conn.execute("""INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,text,token_count,
                    page_start,page_end,hash) VALUES ('c','r','p','text',0,'Jansen Rit',2,1,3,'h')""")
    hit = SearchHit(chunk_id="c", paper_id="p", title="Title", section=None, kind="text", page=1,
                    page_start=1, page_end=3, score=0, snippet="no target text in snippet")
    bench = RetrievalBenchmark.model_validate({"name": "b", "questions": [
        {"id": "q1", "question": "one", "relevant": [
            {"paper": "10.1234/abc", "page": 2, "contains": "JANSEN"},
            {"paper": "1234.5678", "contains": "rit"}, {"paper": "p", "page": 4}]},
        {"id": "q2", "question": "two", "relevant": [{"paper": "p"}]}]})
    calls: list[tuple[int, str | None]] = []

    def retriever(db: sqlite3.Connection, question: str, project_id: str | None = None,
                  k: int = 8) -> list[SearchHit]:
        calls.append((k, project_id))
        if question == "one":
            return [hit.model_copy(update={"paper_id": "other", "chunk_id": "missing"}), hit, hit]
        return []

    report = evaluate_retrieval(conn, bench, k=2, retriever=retriever)
    assert calls == [(2, None), (2, None)]
    assert report.recall_at_k == pytest.approx(1 / 3)
    assert report.hit_rate_at_k == 0.5 and report.mrr == 0.25
    assert report.questions[0].recall == pytest.approx(2 / 3)
    assert report.questions[0].reciprocal_rank == 0.5 and len(report.questions[0].missed) == 1
    row = conn.execute("SELECT * FROM analysis_runs WHERE id=?", (report.run_id,)).fetchone()
    assert row["task"] == "eval_retrieval" and row["status"] == "ok"
    assert json.loads(row["result_json"])["mrr"] == 0.25


def test_answer_and_matrix_metric_arithmetic(conn: sqlite3.Connection) -> None:
    from lens_fakes import gateway, seed

    from litforge.analysis.evaluation import evaluate_answers, evaluate_matrix
    from litforge.analysis.matrix import MatrixColumn, create_matrix, extract_matrix

    span = seed(conn)
    other = seed(conn, "q")
    gw, _ = gateway(conn, {"sentences": [
        {"text": "Gain 3.25.", "kind": "reported_fact", "evidence_ids": [span.id, other.id, "bogus"]},
        {"text": "Missing.", "kind": "unsupported", "evidence_ids": []}], "insufficient": True})
    bench = RetrievalBenchmark.model_validate({"name": "arithmetic", "questions": [
        {"id": "q", "question": "gain", "relevant": [{"paper": "p", "page": 1, "contains": "3.25"}]}],
        "matrix": [{"paper": "p", "column": "gain", "status": "reported", "value": 3.26},
                   {"paper": "q", "column": "gain", "status": "not_reported"}]})
    report = evaluate_answers(conn, gw, bench)
    assert report.citation_precision == pytest.approx(1 / 3)
    assert report.unsupported_sentence_rate == .5
    assert report.invalid_citation_count == 1
    assert conn.execute("SELECT status FROM analysis_runs WHERE id=?", (report.run_id,)).fetchone()[0] == "ok"
    matrix = create_matrix(conn, "proj", "m", "gain", [MatrixColumn(key="gain", label="Gain", instruction="gain",
                                                                               type="number")])
    gw, _ = gateway(conn, {"status": "reported", "value": 3.25, "evidence_ids": [span.id]})
    extract_matrix(conn, gw, matrix.id)
    # Emulate an invalid stored citation to verify the metric checks paper identity.
    conn.execute("UPDATE matrix_cells SET evidence_ids_json=?", (json.dumps([span.id]),))
    metrics = evaluate_matrix(conn, bench, matrix.id)
    assert metrics.status_accuracy == .5 and metrics.value_accuracy == 1 and metrics.citation_validity == .5


def test_matrix_target_rejects_status_typo() -> None:
    from litforge.analysis.evaluation import MatrixTarget

    with pytest.raises(ValidationError):
        MatrixTarget(paper="p", column="gain", status="reproted")


@pytest.mark.parametrize(("kind", "actual", "expected", "accuracy"), [
    ("categorical", "101", "100", 0), ("categorical", "1000", "100", 0),
    ("categorical", "100", "100", 1), ("number", "101", "100", 1),
    ("text", "101", "100", 0), ("text", "1000", "100", 1),
])
def test_matrix_comparison_uses_column_type(conn: sqlite3.Connection, kind: str, actual: str,
                                           expected: str, accuracy: float) -> None:
    from lens_fakes import gateway, seed

    from litforge.analysis.evaluation import evaluate_matrix
    from litforge.analysis.matrix import MatrixColumn, create_matrix, extract_matrix

    span = seed(conn)
    column = MatrixColumn(key="gain", label="Gain", instruction="gain", type=kind,
                          options=["100", "101", "1000"] if kind == "categorical" else None)
    matrix = create_matrix(conn, "proj", "m", "gain", [column])
    gw, _ = gateway(conn, {"status": "reported", "value": actual, "evidence_ids": [span.id]})
    extract_matrix(conn, gw, matrix.id)
    bench = RetrievalBenchmark.model_validate({"name": "typed", "questions": [
        {"id": "q", "question": "gain", "relevant": [{"paper": "p"}]}], "matrix": [
        {"paper": "p", "column": "gain", "status": "reported", "value": expected}]})
    assert evaluate_matrix(conn, bench, matrix.id).value_accuracy == accuracy
