from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from litforge.llm import EvidenceItem, EvidencePacket, LlmError, ModelReply
from litforge.llm.openai_compatible import OpenAICompatibleClient


def item(evidence_id: str = "e1", text: str = "reported result") -> EvidenceItem:
    return EvidenceItem(id=evidence_id, paper_id="p1", text=text, page=3, section="Results")


def test_evidence_packet_requires_items_and_unique_ids() -> None:
    with pytest.raises(ValidationError):
        EvidencePacket(items=[])
    with pytest.raises(ValidationError):
        EvidencePacket(items=[item(), item(text="different")])


def test_evidence_packet_hash_is_deterministic_and_tracks_sensitivity() -> None:
    first = EvidencePacket(items=[item()], sensitive=True)
    second = EvidencePacket(items=[item()], sensitive=True)
    changed = EvidencePacket(items=[item(text="changed")], sensitive=True)
    assert first.content_hash == second.content_hash
    assert first.content_hash != changed.content_hash
    assert first.sensitive is True


class CaptureTransport:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.calls.append((url, headers, body, timeout_s))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_openai_compatible_client_builds_request_and_parses_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-value")
    transport = CaptureTransport({
        "choices": [{"message": {"content": json.dumps({"decision": "include"})}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    })
    client = OpenAICompatibleClient(
        base_url="https://models.example/v1/",
        model="large-model",
        api_key_env="TEST_PROVIDER_KEY",
        transport=transport,
        timeout_s=9,
    )
    reply = client.chat_json([{"role": "user", "content": "question"}], {"type": "object"})
    assert reply == ModelReply(content={"decision": "include"}, provider="openai_compatible",
                               model="large-model", usage={"input_tokens": 12, "output_tokens": 4})
    url, headers, body, timeout = transport.calls[0]
    assert url == "https://models.example/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret-value"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert timeout == 9


def test_openai_compatible_client_requires_key_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    transport = CaptureTransport({})
    client = OpenAICompatibleClient("https://models.example/v1", "m", "MISSING_PROVIDER_KEY", transport)
    with pytest.raises(LlmError, match="MISSING_PROVIDER_KEY"):
        client.chat_json([], {"type": "object"})
    assert transport.calls == []


def test_openai_compatible_errors_do_not_expose_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "never-print-this")
    client = OpenAICompatibleClient(
        "https://models.example/v1", "m", "TEST_PROVIDER_KEY", CaptureTransport(RuntimeError("network failed"))
    )
    with pytest.raises(LlmError) as exc:
        client.chat_json([], {"type": "object"})
    assert "never-print-this" not in str(exc.value)
