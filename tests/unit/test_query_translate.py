from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from litforge.analysis.query_translate import needs_translation, search_query
from litforge.llm.base import LlmError
from litforge.llm.router import CloudApprovalRequired


class FakeGateway:
    def __init__(self, content: Any = None, error: Exception | None = None, model: str = "qwen3:4b") -> None:
        self.content, self.error, self.model, self.calls = content, error, model, []

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
    assert search_query(conn, gateway, QUESTION, approved=True, sensitive=True) == expected  # type: ignore[arg-type]
    task, evidence, messages, kwargs = gateway.calls[0]
    assert task == "query_translate" and evidence.sensitive and kwargs["approved"]
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
])
def test_failures_fall_back_to_the_words_as_written(conn: sqlite3.Connection, gateway: FakeGateway) -> None:
    assert search_query(conn, gateway, QUESTION) == QUESTION  # type: ignore[arg-type]
    assert conn.execute("SELECT COUNT(*) FROM query_translations").fetchone()[0] == 0
