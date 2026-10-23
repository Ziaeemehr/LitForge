from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from litforge.analysis import evaluation
from litforge.analysis.evaluation import (
    RetrievalBenchmark,
    check_benchmark,
    evaluate_claims,
    load_benchmark,
    normalize_text,
)

GOLD = Path(__file__).resolve().parents[2] / "benchmarks" / "sbi10" / "benchmark.yaml"


def test_normalize_ignores_ligatures_case_hyphens_and_whitespace() -> None:
    assert normalize_text("Signiﬁcantly  more\nrobust, learn-\ning") == "significantly more robust, learning"
    assert normalize_text("SNPE-A fails") == "snpe-a fails"
    assert evaluation._contains("a posterior-\nbased method", "posterior-based method")
    assert evaluation._contains("learn-\ning the likelihood", "learning the likelihood")


def test_shipped_benchmark_is_complete() -> None:
    bench = load_benchmark(GOLD)
    english = [q for q in bench.questions if q.lang == "en"]
    assert len(english) == 30 and all(q.answer for q in english)
    assert {q.lang for q in bench.questions} == {"en", "fa"}
    assert sum(len(q.relevant) for q in english) >= 50
    # Paper ids must stay strings: an unquoted 1412.6980 would load as the float 1412.698.
    papers = {t.paper for q in bench.questions for t in q.relevant} | {t.paper for t in bench.screening}
    assert "1412.6980" in papers and len({t.paper for t in bench.screening}) == 10
    assert {r.relation for r in bench.relations} >= {"supports", "contradicts", "qualifies"}
    assert {t.status for t in bench.matrix} == {"reported", "not_reported", "not_applicable"}


@pytest.mark.parametrize("change", [
    {"claims": [{"id": "c", "paper": "p", "page": 1, "contains": "x", "text": "t"}] * 2},
    {"claims": [{"id": "c", "paper": "p", "page": 1, "contains": "x", "text": "t"}],
     "relations": [{"a": "c", "b": "missing", "relation": "supports"}]},
    {"matrix_columns": [{"key": "k", "label": "K", "instruction": "i", "type": "text"}],
     "matrix": [{"paper": "p", "column": "other", "status": "not_reported"}]},
])
def test_benchmark_rejects_dangling_references(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RetrievalBenchmark.model_validate({"name": "b", "questions": [
            {"id": "q", "question": "why", "relevant": [{"paper": "p"}]}], **change})


def _paper(conn: sqlite3.Connection, paper_id: str, arxiv: str, chunks: list[tuple[int, str]]) -> None:
    conn.execute("INSERT INTO papers(id,arxiv_id,created_at,updated_at) VALUES (?,?,'now','now')", (paper_id, arxiv))
    conn.execute("""INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at)
                    VALUES (?,?,?,'f',1,'ok','now')""", ("a" + paper_id, paper_id, "sha" + paper_id))
    conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,created_at)
                    VALUES (?,?,'fake','1','h','needs_docling','now')""", ("r" + paper_id, "a" + paper_id))
    for i, (page, text) in enumerate(chunks):
        conn.execute("""INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,text,token_count,
                        page_start,page_end,hash) VALUES (?,?,?,'text',?,?,1,?,?,'h')""",
                     (f"{paper_id}-{i}", "r" + paper_id, paper_id, i, text, page, page))


def test_check_reports_wrong_pages_missing_quotes_and_papers(conn: sqlite3.Connection) -> None:
    _paper(conn, "p1", "1111.0001", [(2, "SNL is more ro-\nbust than SNPE"), (5, "APT produced tight posteriors")])
    bench = RetrievalBenchmark.model_validate({"name": "b", "questions": [
        {"id": "q1", "question": "one", "relevant": [
            {"paper": "1111.0001", "page": 2, "contains": "more robust than"},
            {"paper": "1111.0001", "page": 4, "contains": "APT produced"},
            {"paper": "1111.0001", "page": 2, "contains": "never written"},
            {"paper": "9999.0001", "page": 1, "contains": "x"}]}]})
    problems = {(p.contains, p.problem) for p in check_benchmark(conn, bench)}
    # A paper whose parse still needs review is indexed all the same, so its quotes count.
    assert problems == {("APT produced", "quote is on page 5"), ("never written", "quote not in indexed text"),
                        ("x", "paper not found")}


def test_claims_found_by_cited_quote_and_relations_by_pair(conn: sqlite3.Connection,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    _paper(conn, "pa", "1111.0001", [])
    _paper(conn, "pb", "1111.0002", [])
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('proj','P','now')")

    def span(paper: str, page: int, text: str) -> SimpleNamespace:
        return SimpleNamespace(paper_id=paper, page_start=page, page_end=page, text=text)

    claims = [SimpleNamespace(id="x1", paper_id="pa", status="proposed"),
              SimpleNamespace(id="x2", paper_id="pb", status="proposed"),
              SimpleNamespace(id="x3", paper_id="pb", status="rejected")]
    spans = {"x1": [span("pa", 1, "SNL is more robust")], "x2": [span("pb", 6, "APT degrades only slightly")],
             "x3": [span("pb", 8, "less accurate")]}
    monkeypatch.setattr(evaluation, "list_claims", lambda c, p: claims)
    monkeypatch.setattr(evaluation, "claim_spans", lambda c, claim: spans[claim.id])
    monkeypatch.setattr(evaluation, "list_relations", lambda c, p: [
        SimpleNamespace(source_claim_id="x2", target_claim_id="x1", relation="qualifies", status="proposed")])
    bench = RetrievalBenchmark.model_validate({
        "name": "b", "questions": [{"id": "q", "question": "why", "relevant": [{"paper": "pa"}]}],
        "claims": [{"id": "c1", "paper": "1111.0001", "page": 1, "contains": "more robust", "text": "t"},
                   {"id": "c2", "paper": "1111.0002", "page": 6, "contains": "degrades only", "text": "t"},
                   {"id": "c3", "paper": "1111.0002", "page": 8, "contains": "less accurate", "text": "t"}],
        "relations": [{"a": "c1", "b": "c2", "relation": "contradicts"},
                      {"a": "c1", "b": "c3", "relation": "contradicts"}]})
    report = evaluate_claims(conn, bench, "proj")
    # c3 is only backed by a rejected claim; the c1/c2 pair exists but with the wrong label.
    assert report.claim_recall == pytest.approx(2 / 3)
    assert [(r.expected, r.actual) for r in report.relations] == [("contradicts", "qualifies"),
                                                                  ("contradicts", None)]
    assert report.relation_accuracy == 0 and report.contradiction_recall == 0 and report.extracted_claims == 2


def test_failed_answer_is_recorded_and_others_scored(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm.base import LlmError

    def ask(conn: object, gateway: object, question: str, **kwargs: object) -> SimpleNamespace:
        if question == "bad":
            raise LlmError("invalid JSON\nresponse")
        return SimpleNamespace(run_id="r1", evidence=[], invalid_citations=["e9"], sentences=[
            SimpleNamespace(kind="unsupported", evidence_ids=[])])

    monkeypatch.setattr(evaluation, "ask", ask)
    bench = RetrievalBenchmark.model_validate({"name": "b", "questions": [
        {"id": "q1", "question": "good", "relevant": [{"paper": "p"}]},
        {"id": "q2", "question": "bad", "relevant": [{"paper": "p"}]}]})
    report = evaluation.evaluate_answers(conn, None, bench)  # type: ignore[arg-type]
    assert report.failed == {"q2": "LlmError: invalid JSON response"} and report.answer_runs == ["r1"]
    assert report.citation_validity == 0 and report.unsupported_sentence_rate == 1
    monkeypatch.setattr(evaluation, "ask", lambda *a, **k: (_ for _ in ()).throw(LlmError("down")))
    with pytest.raises(LlmError, match="every question failed"):
        evaluation.evaluate_answers(conn, None, bench)  # type: ignore[arg-type]
