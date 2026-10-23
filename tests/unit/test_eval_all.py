from __future__ import annotations

import sqlite3

import pytest

from litforge.analysis import eval_all
from litforge.analysis.eval_all import AllReport, Stage, render_report, run_all
from litforge.analysis.evaluation import (
    AnswerReport,
    ClaimMatch,
    ClaimReport,
    LabelProblem,
    QuestionReport,
    RelationMatch,
    RetrievalBenchmark,
    RetrievalReport,
    RetrievalTarget,
)

BENCH = RetrievalBenchmark.model_validate({
    "name": "gold", "questions": [{"id": "q1", "question": "why", "relevant": [{"paper": "1111.0001", "page": 2}]}],
    "matrix_columns": [{"key": "code", "label": "Code", "instruction": "Is code available?", "type": "boolean"}],
    "matrix": [{"paper": "1111.0001", "column": "code", "status": "not_reported"}],
    "criteria": {"question": "q"}, "screening": [{"paper": "1111.0001", "relevant": True}],
    "claims": [{"id": "c1", "paper": "1111.0001", "page": 1, "contains": "x", "text": "t"},
               {"id": "c2", "paper": "1111.0002", "page": 1, "contains": "y", "text": "t"}],
    "relations": [{"a": "c1", "b": "c2", "relation": "contradicts"}]})


def test_report_compares_with_targets_and_lists_misses() -> None:
    retrieval = RetrievalReport(
        run_id="r", name="gold", k=10, recall_at_k=.5, hit_rate_at_k=.5, mrr=.25, recall_by_lang={"en": .75, "fa": 0},
        questions=[QuestionReport(id="q1", question="why", lang="fa", recall=0, hit=False, reciprocal_rank=0,
                                  missed=[RetrievalTarget(paper="1111.0001", page=2)])])
    report = AllReport(
        name="gold", profile="local", k=10, threshold=.05,
        labels=[LabelProblem(item="q1", paper="1111.0001", page=2, contains="z", problem="quote is on page 3")],
        stages=[Stage(name="retrieval", seconds=1.2), Stage(name="matrix", error="LlmError: down"),
                Stage(name="screening", skipped="--skip-models")],
        retrieval=retrieval,
        answers=AnswerReport(run_id="a", name="gold", citation_precision=.4, unsupported_sentence_rate=.2,
                             invalid_citation_count=1, answer_runs=["r"], citation_validity=.9,
                             failed={"q2": "LlmError: invalid JSON"}),
        claims=ClaimReport(run_id="c", name="gold", claim_recall=.5, relation_accuracy=0, contradiction_recall=0,
                           extracted_claims=3, claims=[ClaimMatch(id="c1", found=True, claim_ids=["x"]),
                                                       ClaimMatch(id="c2", found=False, claim_ids=[])],
                           relations=[RelationMatch(a="c1", b="c2", expected="contradicts", actual=None)]))
    text = render_report(report, BENCH)
    assert "- q1: 1111.0001 p.2 “z”: quote is on page 3" in text
    assert "| matrix | 0.0 s | LlmError: down |" in text and "skipped: --skip-models" in text
    assert "| Recall@10 (fa) | 0.0% |  |" in text and "- q1 (fa): 1111.0001 p.2" in text
    assert "| Citations that resolve to retrieved evidence | 90.0% | ≥ 99% ✗ |" in text
    assert "| Questions answered | 1/2 | all |" in text and "- q2 failed: LlmError: invalid JSON" in text
    assert "Claims not found: c2" in text and "- c1–c2: expected contradicts, got no relation" in text


@pytest.fixture()
def project(conn: sqlite3.Connection) -> str:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('proj','P','now')")
    return "proj"


def test_skip_models_runs_labels_and_retrieval_only(conn: sqlite3.Connection, project: str) -> None:
    report = run_all(conn, None, BENCH, project, skip_models=True)  # type: ignore[arg-type]
    assert [(s.name, s.skipped) for s in report.stages] == [
        ("retrieval", None), ("answers", "--skip-models"), ("matrix", "--skip-models"),
        ("screening", "--skip-models"), ("claims", "--skip-models")]
    assert report.retrieval is not None and report.retrieval.recall_at_k == 0
    assert {p.problem for p in report.labels} == {"paper not found"}


def test_failing_stage_is_recorded_and_later_stages_run(conn: sqlite3.Connection, project: str,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        calls.append("answers")
        raise RuntimeError("model\nserver down")

    monkeypatch.setattr(eval_all, "evaluate_answers", boom)
    monkeypatch.setattr(eval_all, "_ensure_matrix", lambda *a: calls.append("matrix") or "m")
    monkeypatch.setattr(eval_all, "extract_matrix", lambda *a: None)
    monkeypatch.setattr(eval_all, "evaluate_matrix", lambda *a: None)
    monkeypatch.setattr(eval_all, "save_criteria", lambda *a: None)
    monkeypatch.setattr(eval_all.fulltext, "screen_fulltext", lambda *a: calls.append("screening"))
    monkeypatch.setattr(eval_all, "evaluate_screening", lambda *a: None)
    monkeypatch.setattr(eval_all.claims, "extract_claims", lambda *a: calls.append("claims"))
    monkeypatch.setattr(eval_all.claim_graph, "cluster_claims", lambda *a: None)
    monkeypatch.setattr(eval_all.claim_graph, "classify_relations", lambda *a: None)
    monkeypatch.setattr(eval_all, "evaluate_claims", lambda *a: None)
    report = run_all(conn, None, BENCH, project, approved=True)  # type: ignore[arg-type]
    assert calls == ["answers", "matrix", "screening", "claims"]
    assert [s.error for s in report.stages if s.error] == ["RuntimeError: model server down"]
