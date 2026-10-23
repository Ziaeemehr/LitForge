from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from litforge.analysis.query_translate import needs_translation, search_query
from litforge.llm.base import LlmError
from litforge.llm.router import CloudApprovalRequired


class FakeGateway:
    def __init__(self, content: Any = None, error: Exception | None = None, model: str = "qwen3:4b",
                 requires_approval: bool = False) -> None:
        self.content, self.error, self.model, self.calls = content, error, model, []
        self.requires_approval = requires_approval

    def preview(self, task: str, evidence: Any, messages: list[dict[str, str]], **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(blocked_reason=None, requires_approval=self.requires_approval or evidence.sensitive
                               and self.model.startswith("cloud"))

    def resolve(self, task: str, profile: str | None = None, vision: bool = False) -> tuple[str, Any]:
        return "local", SimpleNamespace(model=self.model)

    def call(self, task: str, evidence: Any, messages: list[dict[str, str]], schema: dict[str, Any],
             **kwargs: Any) -> SimpleNamespace:
        self.calls.append((task, evidence, messages, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content)


QUESTION = "روش SNL چگونه از سوگیری توزیع پیشنهادی جلوگیری می‌کند؟"
REPLY = {"english": "How does SNL avoid the bias of the proposal distribution?",
         "terms": ["SNL", "sequential neural likelihood", "proposal bias"]}


def test_only_persian_text_is_translated(conn: sqlite3.Connection) -> None:
    gateway = FakeGateway(REPLY)
    assert not needs_translation("How does SNL work?") and needs_translation(QUESTION)
    assert search_query(conn, gateway, "How does SNL work?") == "How does SNL work?"  # type: ignore[arg-type]
    assert search_query(conn, None, QUESTION) == QUESTION
    assert gateway.calls == []


def test_translation_is_appended_cached_and_sent_as_the_query_only(conn: sqlite3.Connection) -> None:
    gateway = FakeGateway(REPLY)
    expected = (QUESTION + "\nHow does SNL avoid the bias of the proposal distribution? SNL "
                "sequential neural likelihood proposal bias")
    assert search_query(conn, gateway, QUESTION, sensitive=True, run_id="r1") == expected  # type: ignore[arg-type]
    task, evidence, messages, kwargs = gateway.calls[0]
    assert task == "query_translate" and evidence.sensitive and kwargs["run_id"] == "r1"
    assert "approved" not in kwargs  # translation never rides on an approval given for another call
    assert [item.text for item in evidence.items] == [QUESTION] and messages[-1]["content"] == QUESTION
    assert search_query(conn, gateway, QUESTION) == expected and len(gateway.calls) == 1  # type: ignore[arg-type]
    # Another model gets its own translation.
    other = FakeGateway({"english": "SNL proposal bias", "terms": []}, model="gemma4")
    assert search_query(conn, other, QUESTION).endswith("\nSNL proposal bias")  # type: ignore[arg-type]


@pytest.mark.parametrize("gateway", [
    FakeGateway(error=LlmError("invalid JSON response from provider")),
    FakeGateway(error=CloudApprovalRequired("approval required")),
    FakeGateway({"english": "", "terms": []}),
    FakeGateway({"english": "هنوز فارسی", "terms": []}),  # an untranslated reply would only repeat the question
    FakeGateway("not an object"),
    FakeGateway(REPLY, requires_approval=True),  # ask-before-cloud route: not used, never asks
])
def test_failures_fall_back_to_the_words_as_written(conn: sqlite3.Connection, gateway: FakeGateway) -> None:
    assert search_query(conn, gateway, QUESTION) == QUESTION  # type: ignore[arg-type]
    assert conn.execute("SELECT COUNT(*) FROM query_translations").fetchone()[0] == 0


def test_sensitive_text_is_not_translated_on_a_route_that_needs_approval(conn: sqlite3.Connection) -> None:
    gateway = FakeGateway(REPLY, model="cloud/model")
    assert search_query(conn, gateway, QUESTION, sensitive=True) == QUESTION  # type: ignore[arg-type]
    assert gateway.calls == []
    assert search_query(conn, gateway, QUESTION) != QUESTION  # type: ignore[arg-type]


def test_persian_question_asks_for_a_persian_answer_and_carries_the_translation(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.analysis import prompts
    from litforge.analysis.prompts import ask_messages
    from litforge.llm.evidence import EvidenceItem, EvidencePacket
    monkeypatch.setattr(prompts, "evidence_text", lambda packet, spans: "<evidence/>")
    packet = EvidencePacket(items=[EvidenceItem(id="e1", paper_id="p", text="SNL learns the likelihood.", page=1)])
    system, user = ask_messages(QUESTION, packet, [], "How does SNL avoid proposal bias?")
    assert "write every sentence of the answer in Persian" in system["content"]
    assert "never repeat or restate the question" in system["content"]
    assert user["content"].startswith("Question: How does SNL avoid proposal bias?\nAsked in Persian as: " + QUESTION)
    english = ask_messages("How does SNL work?", packet, [])
    assert "never repeat" not in english[0]["content"] and english[1]["content"].startswith("How does SNL work?\n")


def test_an_answer_that_repeats_the_question_is_not_presented_as_an_answer(conn: sqlite3.Connection) -> None:
    from lens_fakes import gateway, seed
    span = seed(conn, text="Results: SNL learns the likelihood with a flow.")
    gw, _ = gateway(conn, {"sentences": [{"text": "What does SNL learn?", "kind": "reported_fact",
                                          "evidence_ids": [span.id], "quotes": []}], "insufficient": False})
    from litforge.analysis.ask import ask
    answer = ask(conn, gw, "What does SNL learn?")
    assert [s.text for s in answer.sentences] == ["The model repeated the question instead of answering it."]
    assert answer.sentences[0].kind == "unsupported"
