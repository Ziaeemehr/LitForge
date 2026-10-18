from __future__ import annotations

import json
import sqlite3

import pytest
from lens_fakes import gateway, seed

from litforge.analysis.ask import ask, get_answer
from litforge.analysis.audit import audit_answer, plan_calls
from litforge.analysis.prompts import AUDIT_PROMPT_VERSION
from litforge.analysis.review import review_queue


@pytest.mark.parametrize("verdict", ["supported", "partial", "unsupported", "contradicted"])
def test_verdict_storage_and_queue(conn: sqlite3.Connection, verdict: str) -> None:
    span = seed(conn)
    gw, _ = gateway(conn, {"sentences": [
        {"text": "Gain 3.25.", "kind": "reported_fact", "evidence_ids": [span.id]},
        {"text": "Unknown.", "kind": "unsupported", "evidence_ids": []}], "insufficient": True})
    answer = ask(conn, gw, "gain", sensitive=True)
    auditor, fake = gateway(conn, {"verdict": verdict, "note": "checked"})
    previews = plan_calls(conn, auditor, answer.run_id)
    assert len(previews) == 1 and fake.calls == 0
    sentences = audit_answer(conn, auditor, answer.run_id)
    assert fake.calls == 1
    assert sentences[0].audit_verdict == verdict and sentences[0].audit_note == "checked"
    assert sentences[1].audit_verdict == "unsupported"
    assert len(review_queue(conn)) == (1 if verdict == "supported" else 2)
    assert get_answer(conn, answer.run_id).sentences == sentences
    call = conn.execute("SELECT * FROM model_calls WHERE task='audit'").fetchone()
    run = conn.execute("SELECT * FROM analysis_runs WHERE id=?", (call["run_id"],)).fetchone()
    assert run["task"] == "audit" and json.loads(run["config_json"])["answer_run"] == answer.run_id
    assert call["prompt_version"] == AUDIT_PROMPT_VERSION


def test_audit_sees_only_cited_spans(conn: sqlite3.Connection) -> None:
    from litforge.screening.llm import FakeLlm

    first = seed(conn)
    second = seed(conn, "q")
    gw, _ = gateway(conn, {"sentences": [{"text": "Gain 3.25.", "kind": "reported_fact",
                                        "evidence_ids": [first.id]}], "insufficient": False})
    answer = ask(conn, gw, "gain")
    assert len(answer.evidence) == 2
    def reply(messages: list[dict[str, str]]) -> dict[str, str]:
        assert first.id in messages[-1]["content"] and second.id not in messages[-1]["content"]
        assert "untrusted" in messages[0]["content"] and "never invent" in messages[0]["content"]
        return {"verdict": "partial", "note": "checked"}
    gw.providers["local"] = FakeLlm(reply)
    audit_answer(conn, gw, answer.run_id)


@pytest.mark.parametrize("verdict", ["partial", "unsupported", "contradicted"])
def test_changed_failing_audit_reopens_review(conn: sqlite3.Connection, verdict: str) -> None:
    from litforge.analysis.review import record_review

    seed(conn)
    gw, _ = gateway(conn)
    answer = ask(conn, gw, "gain")
    auditor, fake = gateway(conn, {"verdict": "supported", "note": "checked"})
    audit_answer(conn, auditor, answer.run_id)
    sentence_id = answer.sentences[0].id
    record_review(conn, "sentence", sentence_id, "accept", reason="checked")
    history = dict(conn.execute("SELECT * FROM human_reviews").fetchone())
    assert review_queue(conn) == []
    fake.answer = lambda _: {"verdict": verdict, "note": "new finding"}
    sentences = audit_answer(conn, auditor, answer.run_id)
    assert sentences[0].review_status == "open"
    assert get_answer(conn, answer.run_id).sentences == sentences
    assert [i.target_id for i in review_queue(conn)] == [sentence_id]
    assert dict(conn.execute("SELECT * FROM human_reviews").fetchone()) == history
    record_review(conn, "sentence", sentence_id, "accept")
    assert audit_answer(conn, auditor, answer.run_id)[0].review_status == "accepted"
