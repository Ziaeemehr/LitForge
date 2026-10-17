from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import pytest

from litforge.analysis.gateway import GatewayError, ModelGateway
from litforge.analysis.runs import start_run
from litforge.config import Config, ModelProfileConfig
from litforge.llm import EvidenceItem, EvidencePacket, LlmError, LlmUnavailable, ModelReply, ModelUsage
from litforge.llm.audit import ModelCallRecord, record_model_call
from litforge.llm.budget import BudgetExceeded, Pricing
from litforge.llm.openai_compatible import OpenAICompatibleClient
from litforge.llm.router import CloudApprovalRequired, RoutingError
from litforge.screening.llm import FakeLlm, OllamaClient

MESSAGES = [{"role": "user", "content": "12345"}]
SCHEMA = {"type": "object"}


@pytest.fixture()
def packet() -> EvidencePacket:
    return EvidencePacket(items=[EvidenceItem(id="ev_1", paper_id="p", text="quote", page=1)])


@pytest.fixture()
def cfg() -> Config:
    config = Config()
    config.models.policy = "hybrid"
    config.privacy.allow_cloud = True
    config.models.profiles["remote"] = ModelProfileConfig(
        provider="openai_compatible", model="large", remote=True,
        pricing=Pricing(input_per_million_usd=1, output_per_million_usd=2),
    )
    return config


class ReplyProvider:
    def __init__(self, reply: ModelReply | Exception) -> None:
        self.reply = reply
        self.calls = 0

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> ModelReply:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_local_normalisation_hash_and_lazy_cache(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    fake = FakeLlm(lambda _: {"ok": True})
    made: list[str] = []

    def factory(name: str, profile: ModelProfileConfig) -> FakeLlm:
        made.append(name)
        return fake

    config = Config()
    gateway = ModelGateway(conn, config, provider_factory=factory)
    preview = gateway.preview("extract", packet, MESSAGES)
    assert preview.estimated_input_tokens == 2 and preview.max_cost_usd == 0
    assert made == []
    run = start_run(conn, "extract", None, {"q": "x"}, {})
    for _ in range(2):
        reply = gateway.call("extract", packet, MESSAGES, SCHEMA, run_id=run, prompt_version="v1")
        assert reply.content == {"ok": True} and reply.usage == ModelUsage()
        assert reply.provider == "ollama" and reply.model == config.models.local.model
    assert made == ["local"] and fake.calls == 2
    rows = conn.execute("SELECT * FROM model_calls").fetchall()
    assert len(rows) == 2 and rows[0]["status"] == "ok" and rows[0]["cost_usd"] == 0
    assert rows[0]["run_id"] == run and rows[0]["prompt_version"] == "v1"
    payload = dict(messages=MESSAGES, schema=SCHEMA, evidence_hash=packet.content_hash,
                   model=config.models.local.model, provider="ollama")
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
    assert rows[0]["input_hash"] == expected
    assert json.loads(rows[0]["evidence_ids_json"]) == ["ev_1"]


@pytest.mark.parametrize("reason", ["local_only", "cloud", "sensitive", "pricing", "budget", "unknown"])
def test_checks_precede_factory(conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket, reason: str) -> None:
    def forbidden(name: str, profile: ModelProfileConfig) -> FakeLlm:
        pytest.fail("provider factory accessed before guards passed")

    error: type[Exception] = RoutingError
    approved = True
    profile = "remote"
    if reason == "local_only":
        cfg.models.policy = "local_only"
    elif reason == "cloud":
        cfg.models.policy = "ask_before_cloud"
        cfg.privacy.allow_cloud = False
    elif reason == "sensitive":
        packet = packet.model_copy(update={"sensitive": True})
        approved = False
        error = CloudApprovalRequired
    elif reason == "pricing":
        cfg.models.profiles["remote"].pricing = None
        error = BudgetExceeded
    elif reason == "budget":
        cfg.budget.max_per_call_usd = 0
        error = BudgetExceeded
    else:
        profile = "missing"
    gateway = ModelGateway(conn, cfg, provider_factory=forbidden)
    with pytest.raises(error):
        gateway.call("extract", packet, MESSAGES, SCHEMA, profile=profile, approved=approved)
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


def test_approval_and_hybrid_local_fallback(conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket) -> None:
    remote = FakeLlm(lambda _: {})
    local = FakeLlm(lambda _: {})
    gateway = ModelGateway(conn, cfg, {"remote": remote, "local": local})
    cfg.models.policy = "ask_before_cloud"
    with pytest.raises(CloudApprovalRequired):
        gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote")
    gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote", approved=True)
    cfg.models.policy = "hybrid"
    cfg.privacy.allow_cloud = False
    gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote")
    assert remote.calls == local.calls == 1


@pytest.mark.parametrize("limit", ["run", "month"])
def test_spend_from_audit(conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket, limit: str) -> None:
    run = start_run(conn, "t", None, {}, {})
    for cost, run_id in [(0.7, run), (0.7, "other")]:
        record_model_call(conn, ModelCallRecord(provider="p", model="m", task="t", input_hash="h",
                                               cost_usd=cost, status="ok", run_id=run_id))
    old = record_model_call(conn, ModelCallRecord(provider="p", model="m", task="t", input_hash="h",
                                                 cost_usd=100, status="ok"))
    conn.execute("UPDATE model_calls SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old,))
    cfg.budget.max_per_call_usd = 1
    cfg.budget.max_per_run_usd = 0.8 if limit == "run" else 10
    cfg.budget.max_per_month_usd = 1.5 if limit == "month" else 10
    fake = FakeLlm(lambda _: {})
    gateway = ModelGateway(conn, cfg, {"remote": fake})
    with pytest.raises(BudgetExceeded, match=limit):
        gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote", run_id=run, max_output_tokens=100_000)
    assert fake.calls == 0
    cfg.budget.max_per_run_usd = cfg.budget.max_per_month_usd = 10
    gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote", run_id=run, max_output_tokens=100_000)
    assert fake.calls == 1


@pytest.mark.parametrize("status", ["ok", "error", "over_budget"])
def test_dispatch_always_audited(conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket,
                               status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYSIS_TEST_SECRET", "secret-never-record")
    cfg.models.profiles["remote"].api_key_env = "ANALYSIS_TEST_SECRET"
    reply = ModelReply(content={"ok": True}, provider="openai_compatible", model="large",
                       usage=ModelUsage(input_tokens=10, output_tokens=20 if status == "ok" else 1_000_000))
    provider = ReplyProvider(LlmError("Authorization: Bearer secret-never-record") if status == "error" else reply)
    gateway = ModelGateway(conn, cfg, {"remote": provider})
    if status == "ok":
        assert gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote") == reply
    else:
        with pytest.raises(LlmError if status == "error" else BudgetExceeded) as exc:
            gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote")
        assert "secret-never-record" not in str(exc.value) and "Authorization" not in str(exc.value)
    row = conn.execute("SELECT * FROM model_calls").fetchone()
    assert row["status"] == status and provider.calls == 1
    assert "secret-never-record" not in str(dict(row)) and "Authorization" not in str(dict(row))
    if status != "error":
        assert row["cost_usd"] == pytest.approx((10 + reply.usage.output_tokens * 2) / 1_000_000)


@pytest.mark.parametrize("reason", ["local_only policy", "cloud disabled", "missing pricing", "over budget", None])
def test_preview_never_dispatches(conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket,
                                 reason: str | None) -> None:
    cfg.models.policy = "ask_before_cloud"
    if reason == "local_only policy":
        cfg.models.policy = "local_only"
    elif reason == "cloud disabled":
        cfg.privacy.allow_cloud = False
    elif reason == "missing pricing":
        cfg.models.profiles["remote"].pricing = None
    elif reason == "over budget":
        cfg.budget.max_per_call_usd = 0
    gateway = ModelGateway(conn, cfg)
    preview = gateway.preview("t", packet, MESSAGES, profile="remote")
    assert preview.blocked_reason == reason
    assert preview.requires_approval == (reason != "local_only policy")
    assert preview.evidence_ids == ["ev_1"]
    assert preview.max_cost_usd is None if reason == "missing pricing" else preview.max_cost_usd is not None
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


def test_unknown_provider_typed_error(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    cfg = Config()
    cfg.models.local.provider = "unknown"
    with pytest.raises(GatewayError):
        ModelGateway(conn, cfg).call("t", packet, MESSAGES, SCHEMA)


@pytest.mark.parametrize("kind", [LlmUnavailable, LlmError, RuntimeError])
def test_factory_errors_are_safe_and_not_dispatched(conn: sqlite3.Connection, packet: EvidencePacket,
                                                  kind: type[Exception]) -> None:
    def factory(name: str, profile: ModelProfileConfig) -> FakeLlm:
        raise kind("Authorization: Bearer private-secret")

    with pytest.raises(kind if issubclass(kind, LlmError) else GatewayError) as exc:
        ModelGateway(conn, Config(), provider_factory=factory).call("t", packet, MESSAGES, SCHEMA)
    assert "private-secret" not in str(exc.value) and "Authorization" not in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


@pytest.mark.parametrize("limit", ["run", "month"])
def test_actual_cost_checks_cumulative_spend(conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket,
                                           limit: str) -> None:
    run = start_run(conn, "t", None, {}, {})
    record_model_call(conn, ModelCallRecord(provider="p", model="m", task="t", input_hash="h", status="ok",
                                           run_id=run, cost_usd=0.9))
    cfg.budget.max_per_run_usd = 1 if limit == "run" else 10
    cfg.budget.max_per_month_usd = 1 if limit == "month" else 10
    reply = ModelReply(content={}, provider="p", model="m", usage=ModelUsage(output_tokens=100_000))
    gateway = ModelGateway(conn, cfg, {"remote": ReplyProvider(reply)})
    with pytest.raises(BudgetExceeded, match=limit):
        gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote", run_id=run, max_output_tokens=1)
    row = conn.execute("SELECT * FROM model_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["status"] == "over_budget" and row["cost_usd"] == pytest.approx(0.2)


def test_sensitive_router_approval_remains_strict(conn: sqlite3.Connection, cfg: Config,
                                               packet: EvidencePacket) -> None:
    cfg.privacy.require_confirmation_for_sensitive_content = False
    fake = FakeLlm(lambda _: {})
    gateway = ModelGateway(conn, cfg, {"remote": fake})
    sensitive = EvidencePacket(items=packet.items, sensitive=True)
    assert gateway.preview("t", sensitive, MESSAGES, profile="remote").requires_approval
    with pytest.raises(CloudApprovalRequired):
        gateway.call("t", sensitive, MESSAGES, SCHEMA, profile="remote")
    gateway.call("t", sensitive, MESSAGES, SCHEMA, profile="remote", approved=True)
    assert fake.calls == 1


def test_default_ollama_factory_uses_environment(conn: sqlite3.Connection, packet: EvidencePacket,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITFORGE_OLLAMA_URL", "http://fake.local")
    captured: list[tuple[str, dict[str, Any] | None]] = []

    def call(client: OllamaClient, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        assert client.url == "http://fake.local"
        captured.append((path, body))
        return {"message": {"content": '{"ok": true}'}}

    monkeypatch.setattr(OllamaClient, "_call", call)
    gateway = ModelGateway(conn, Config())
    reply = gateway.call("t", packet, MESSAGES, SCHEMA, max_output_tokens=12)
    assert reply.content == {"ok": True}
    assert captured[0][0] == "/api/chat"
    assert captured[0][1]["options"]["num_predict"] == 12


def test_default_openai_factory_and_output_cap(conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm.openai_compatible import UrllibTransport

    cfg.models.profiles["remote"].base_url = "https://fake.example/v1"
    cfg.models.profiles["remote"].api_key_env = "ANALYSIS_FAKE_KEY"
    monkeypatch.setenv("ANALYSIS_FAKE_KEY", "fake-value")
    captured: list[dict[str, Any]] = []

    def request(transport: UrllibTransport, url: str, headers: dict[str, str], body: dict[str, Any],
                timeout_s: float) -> dict[str, Any]:
        assert url == "https://fake.example/v1/chat/completions"
        captured.append(body)
        return {"choices": [{"message": {"content": "{}"}}], "usage": {"completion_tokens": 2}}

    monkeypatch.setattr(UrllibTransport, "request", request)
    gateway = ModelGateway(conn, cfg)
    gateway.call("t", packet, MESSAGES, SCHEMA, profile="remote", max_output_tokens=12)
    assert isinstance(gateway.providers["remote"], OpenAICompatibleClient)
    assert captured[0]["max_tokens"] == 12


@pytest.mark.parametrize("error", [
    LlmError("model qwen3:4b is not pulled; run `ollama pull qwen3:4b`"),
    LlmUnavailable("Ollama unavailable; start ollama serve"),
])
def test_local_provider_errors_keep_their_hint(
    conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket, error: LlmError,
) -> None:
    provider = ReplyProvider(error)
    with pytest.raises(type(error)) as raised:
        ModelGateway(conn, cfg, {"local": provider}).call("t", packet, MESSAGES, SCHEMA)
    assert str(raised.value) == str(error)
    row = conn.execute("SELECT status, error FROM model_calls").fetchone()
    assert row["status"] == "error" and row["error"] == str(error)


@pytest.mark.parametrize("message", [
    "invalid JSON from model: token=secret-token-123",
    "HTTP 500 body: token=secret-token-123",
    "model qwen3 is not pulled; run `ollama pull qwen3` token=secret-token-123",
    "model qwen3 is not pulled; run `ollama pull qwen3`\n",
])
def test_local_provider_errors_redact_output(
    conn: sqlite3.Connection, cfg: Config, packet: EvidencePacket, message: str,
) -> None:
    provider = ReplyProvider(LlmError(message))
    with pytest.raises(LlmError) as raised:
        ModelGateway(conn, cfg, {"local": provider}).call("t", packet, MESSAGES, SCHEMA)
    assert str(raised.value) == "model provider failed"
    row = conn.execute("SELECT status, error FROM model_calls").fetchone()
    assert row["status"] == "error" and row["error"] == "model provider failed"
