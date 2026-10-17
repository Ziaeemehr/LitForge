from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from litforge.config import ModelProfileConfig, ModelsConfig, load_config
from litforge.llm import EvidenceItem, EvidencePacket, LlmError, ModelReply
from litforge.llm.budget import BudgetExceeded, BudgetGuard, Pricing
from litforge.llm.openai_compatible import OpenAICompatibleClient
from litforge.llm.router import CloudApprovalRequired, ModelProfile, ModelRequest, ModelRouter, RoutingError
from litforge.screening.llm import reply_content


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
    assert client.name == "large-model"
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


def profiles() -> dict[str, ModelProfile]:
    return {
        "local": ModelProfile(name="local", provider="ollama", model="small", remote=False),
        "large": ModelProfile(name="large", provider="openai_compatible", model="large", remote=True),
    }


def test_router_enforces_local_only_before_remote_dispatch() -> None:
    router = ModelRouter(profiles(), local_profile="local")
    request = ModelRequest(task="synthesis", evidence=EvidencePacket(items=[item()]), preferred_profile="large",
                           allow_cloud=True)
    with pytest.raises(RoutingError, match="local_only"):
        router.route(request, policy="local_only")


def test_router_requires_approval_and_blocks_sensitive_hybrid() -> None:
    router = ModelRouter(profiles(), local_profile="local")
    normal = ModelRequest(task="synthesis", evidence=EvidencePacket(items=[item()]), preferred_profile="large",
                          allow_cloud=True)
    with pytest.raises(CloudApprovalRequired):
        router.route(normal, policy="ask_before_cloud")
    assert router.route(normal, policy="ask_before_cloud", approved=True).profile.name == "large"
    sensitive = normal.model_copy(update={"evidence": EvidencePacket(items=[item()], sensitive=True)})
    with pytest.raises(CloudApprovalRequired):
        router.route(sensitive, policy="hybrid")
    assert router.route(sensitive, policy="hybrid", approved=True).profile.name == "large"


def test_hybrid_requires_task_cloud_permission() -> None:
    router = ModelRouter(profiles(), local_profile="local")
    request = ModelRequest(task="extract", evidence=EvidencePacket(items=[item()]), preferred_profile="large")
    assert router.route(request, policy="hybrid").profile.name == "local"


def test_budget_guard_requires_prices_and_enforces_call_and_run_limits() -> None:
    guard = BudgetGuard(max_per_call_usd=1, max_per_run_usd=2, max_per_month_usd=3)
    with pytest.raises(BudgetExceeded, match="pricing"):
        guard.check(None, input_tokens=1, max_output_tokens=1, spent_usd=0)
    pricing = Pricing(input_per_million_usd=10, output_per_million_usd=20)
    assert guard.check(pricing, input_tokens=10_000, max_output_tokens=10_000, spent_usd=0) == pytest.approx(0.3)
    with pytest.raises(BudgetExceeded, match="per-call"):
        guard.check(pricing, input_tokens=100_000, max_output_tokens=100_000, spent_usd=0)
    with pytest.raises(BudgetExceeded, match="run"):
        guard.check(pricing, input_tokens=10_000, max_output_tokens=10_000, spent_usd=1.8)
    with pytest.raises(BudgetExceeded, match="month"):
        guard.check(pricing, input_tokens=10_000, max_output_tokens=10_000, spent_usd=0,
                    spent_month_usd=2.8)
    assert guard.check_actual(pricing, input_tokens=10_000, output_tokens=5_000,
                              spent_usd=0, spent_month_usd=0) == pytest.approx(0.2)


def test_default_model_policy_is_local_and_cloud_disabled() -> None:
    config = load_config()
    assert config.models.policy == "local_only"
    assert config.models.local.provider == "ollama"
    assert config.privacy.allow_cloud is False
    assert config.budget.max_per_run_usd == 2


def test_named_remote_profiles_are_configurable() -> None:
    config = ModelsConfig(profiles={
        "reasoning": ModelProfileConfig(
            provider="openai_compatible",
            model="reasoning-model",
            remote=True,
            base_url="https://models.example/v1",
            api_key_env="REASONING_API_KEY",
        )
    })
    assert config.profiles["reasoning"].api_key_env == "REASONING_API_KEY"


def test_screening_boundary_accepts_provider_reply() -> None:
    reply = ModelReply(content={"decision": "include"}, provider="p", model="m")
    assert reply_content(reply) == {"decision": "include"}
    assert reply_content({"decision": "exclude"}) == {"decision": "exclude"}
