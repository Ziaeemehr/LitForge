"""The Write view's assistant: answers about the document and the project's papers, with verified quotes, and
edits that go through the same accept/reject suggestions and guards as the other AI actions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lens_fakes import seed

from litforge.analysis.gateway import ModelGateway
from litforge.config import Config, Paths
from litforge.screening.llm import FakeLlm
from litforge.storage.db import open_db
from litforge.writing import chat as wchat
from litforge.writing import documents as wd
from litforge.writing.bib import citation_key


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    paths.ensure()
    conn = open_db(paths.db)
    span = seed(conn, "p1", "Results: the coupling gain was 3.25 across 40 subjects in the whole-brain model.")
    conn.execute("UPDATE papers SET authors_json=?, year=2021 WHERE id='p1'", (json.dumps(["Ann Lee"]),))
    doc = wd.create_document(conn, paths, "demo", "Draft")
    yield conn, paths, doc, span
    conn.close()


def run(env, question: str, reply: Any, selection: str = "", history: list[dict[str, str]] | None = None):
    conn, paths, doc, _ = env
    seen: list[list[dict[str, str]]] = []
    fake = FakeLlm(lambda messages: seen.append(messages) or (reply(messages) if callable(reply) else reply))
    gateway = ModelGateway(conn, Config(), providers={"local": fake})
    req = wchat.prepare(conn, doc, question, selection=selection, history=history or [])
    return wchat.run(conn, gateway, doc, req, "main.tex"), seen, req


def evidence_id(messages: list[dict[str, str]]) -> str:
    import re
    return re.search(r'<evidence id="([^"]+)"', messages[-1]["content"]).group(1)


def test_answer_with_a_verified_quote_becomes_a_citation_key(env) -> None:
    conn, *_ = env
    result, seen, _ = run(env, "What coupling gain was used in whole-brain models?", lambda m: {
        "answer": "A gain of 3.25 was used.", "citations": [{"evidence_id": evidence_id(m),
                                                              "quote": "the coupling gain was 3.25"}]})
    assert result["answer"] == "A gain of 3.25 was used."
    assert [c["key"] for c in result["citations"]] == [citation_key(conn, "p1")]
    assert result["citations"][0]["quote"] == "the coupling gain was 3.25" and result["citations"][0]["page"]
    assert result["flags"] == [] and result["suggestion"] is None
    assert seen[0][0]["role"] == "system" and "untrusted" in seen[0][0]["content"].lower()


def test_a_quote_not_in_the_evidence_is_dropped_and_flagged(env) -> None:
    result, _, _ = run(env, "What coupling gain was used?", lambda m: {
        "answer": "A gain of 9 was used.", "citations": [{"evidence_id": evidence_id(m), "quote": "a gain of nine"}]})
    assert result["citations"] == []
    assert any("not found in the evidence" in f for f in result["flags"])


def test_an_edit_of_the_selection_becomes_a_guarded_suggestion(env) -> None:
    selection = "We used a gain of 3.25 \\cite{old2020}."
    result, seen, _ = run(env, "Make this sentence say how many subjects there were", lambda m: {
        "answer": "Added the number of subjects.", "citations": [],
        "replacement": "We used a gain of 3.25 in 40 subjects and 12 sites \\cite{old2020}."}, selection=selection)
    suggestion = result["suggestion"]
    assert suggestion is not None and suggestion["original"] == selection and suggestion["task"] == "write_chat"
    flagged = [f for h in suggestion["hunks"] for f in h["flags"]]
    assert any("12" in f for f in flagged)          # invented: not in the selection or the evidence
    assert not any(": 40" in f for f in flagged)    # 40 is in the evidence
    assert selection in seen[0][-1]["content"]


def test_without_a_selection_no_edit_is_offered(env) -> None:
    result, _, _ = run(env, "Rewrite my introduction", {"answer": "Select the text first.", "citations": [],
                                                        "replacement": "Something new"})
    assert result["suggestion"] is None


def test_a_question_without_matching_evidence_still_gets_an_answer(env) -> None:
    result, seen, req = run(env, "How do I make a table in LaTeX?", {"answer": "Use tabular.", "citations": []})
    assert result["answer"] == "Use tabular." and result["citations"] == []
    assert "<evidence" not in seen[0][-1]["content"]


def test_history_is_sent_in_order_and_limited(env) -> None:
    history = [{"role": "user", "text": f"q{i}"} if i % 2 == 0 else {"role": "assistant", "text": f"a{i}"}
               for i in range(20)]
    _, seen, _ = run(env, "and now?", {"answer": "ok", "citations": []}, history=history)
    roles = [m["role"] for m in seen[0][1:-1]]
    assert len(roles) <= wchat.MAX_HISTORY and roles[0] == "user" and roles[-1] == "assistant"
    assert seen[0][-2]["content"] == "a19"


@pytest.mark.parametrize("question, selection", [("", ""), ("x" * (wchat.MAX_QUESTION + 1), ""),
                                                 ("ok", "y" * (wchat.MAX_SELECTION + 1))])
def test_limits(env, question: str, selection: str) -> None:
    conn, paths, doc, _ = env
    with pytest.raises(wchat.ChatError):
        wchat.prepare(conn, doc, question, selection=selection)


def test_with_a_selection_the_reply_must_carry_a_replacement_field(env) -> None:
    # qwen3:8b put a shortened sentence in the answer, so no suggestion could be offered (real run, 2026-10-19)
    conn, paths, doc, _ = env
    with_selection = wchat.prepare(conn, doc, "Shorter please", selection="A long sentence here.")
    without = wchat.prepare(conn, doc, "What is TVB?")
    assert "replacement" in wchat.schema_for(with_selection)["required"]
    # the model writes fields in schema order: replacement first made qwen3:8b fill it (real run)
    assert list(wchat.schema_for(with_selection)["properties"])[0] == "replacement"
    assert "replacement" not in wchat.schema_for(without)["required"]
    assert "must put the whole new text in replacement" in with_selection.messages[0]["content"]


def test_an_lr_selection_is_edited_inside_its_wrapper(env) -> None:
    selection = "\\lr{The model that we use was fitted to the data of 40 patients.}"
    result, seen, _ = run(env, "Shorter please", lambda m: {"answer": "Shortened.", "citations": [],
                                                            "replacement": "The model fits 40 patients."},
                          selection=selection)
    assert "\\lr{" not in seen[0][-1]["content"]
    suggestion = result["suggestion"]
    accepted = [h["index"] for h in suggestion["hunks"]]
    from litforge.writing import assist as wa
    assert wa.applied_text(selection, suggestion["hunks"], accepted) == "\\lr{The model fits 40 patients.}"
    assert suggestion["flags"] == []
