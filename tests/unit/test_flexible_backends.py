from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from litforge.analysis.gateway import GatewayError, ModelGateway, VisionUnsupported
from litforge.analysis.runs import start_run
from litforge.cli import app
from litforge.config import Config, ModelProfileConfig, ModelsConfig
from litforge.llm import EvidenceItem, EvidencePacket, LlmError
from litforge.llm.budget import BudgetExceeded, Pricing
from litforge.llm.openai_compatible import OpenAICompatibleClient
from litforge.llm.router import RoutingError
from litforge.screening.llm import OllamaClient

MESSAGES = [{"role": "user", "content": "test"}]
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class Transport:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents or ('{"ok":true}',))
        self.calls: list[tuple[dict[str, str], dict[str, Any]]] = []

    def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.calls.append((headers, body))
        return {"choices": [{"message": {"content": self.contents[min(len(self.calls)-1, len(self.contents)-1)],
                                          "reasoning_content": "private thinking"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5}}


@pytest.fixture()
def packet() -> EvidencePacket:
    return EvidencePacket(items=[EvidenceItem(id="e1", paper_id="p", text="quote", page=1)])


def test_task_resolution_and_reasoning(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    cfg = Config(models={"profiles": {"llama": {"provider": "openai_compatible", "model": "gemma",
                          "base_url": "http://127.0.0.1:8080/v1", "vision": True}},
                         "tasks": {"ask": {"profile": "llama", "reasoning": "on"},
                                   "asset_analyze": {"profile": "llama"}}})
    gateway = ModelGateway(conn, cfg)
    preview = gateway.preview("ask", packet, MESSAGES)
    assert (preview.profile, preview.reasoning, preview.from_task_map, preview.max_output_tokens) == (
        "llama", "on", True, 4096)
    assert gateway.preview("ask", packet, MESSAGES, "local").profile == "local"
    assert gateway.preview("ask", packet, MESSAGES, "local").reasoning == "on"
    assert gateway.preview_vision("asset_analyze", packet, MESSAGES).profile == "llama"
    assert gateway.preview_vision("other", packet, MESSAGES).profile == "local_vision"


@pytest.mark.parametrize("reason", ["policy", "cloud", "approval", "sensitive", "pricing", "budget",
                                    "vision", "context", "unknown", "key"])
@pytest.mark.parametrize("call_mode", ["text", "vision", "vision_text"])
def test_no_transport_before_guards(conn: sqlite3.Connection, packet: EvidencePacket, reason: str,
                                    call_mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITFORGE_MISSING_TEST_KEY", "fake-test-value")
    if reason == "key":
        monkeypatch.delenv("LITFORGE_MISSING_TEST_KEY")
    cfg = Config()
    cfg.models.policy = "ask_before_cloud"
    cfg.privacy.allow_cloud = True
    cfg.models.profiles["remote"] = ModelProfileConfig(
        provider="openai_compatible", model="model", remote=True, base_url="https://fake.invalid/v1",
        api_key_env="LITFORGE_MISSING_TEST_KEY", vision=True,
        pricing=Pricing(input_per_million_usd=1, output_per_million_usd=2))
    cfg.models.tasks = {"ask": {"profile": "remote"}}
    # Revalidate tasks assigned above, just as YAML loading does.
    cfg = Config.model_validate(cfg.model_dump())
    profile = cfg.models.profiles["remote"]
    transport = Transport()
    client = OpenAICompatibleClient(profile.base_url or "", profile.model, profile.api_key_env, transport=transport)
    approved = reason != "approval"
    explicit = None
    if reason == "policy":
        cfg.models.policy = "local_only"
    elif reason == "cloud":
        cfg.privacy.allow_cloud = False
        cfg.models.policy = "hybrid"
    elif reason == "sensitive":
        packet.sensitive = True
        approved = False
    elif reason == "pricing":
        profile.pricing = None
    elif reason == "budget":
        cfg.budget.max_per_call_usd = 0
    elif reason == "vision":
        profile.vision = False
    elif reason == "context":
        profile.context_tokens = 10
    elif reason == "unknown":
        explicit = "missing"
    gateway = ModelGateway(conn, cfg, {"remote": client})
    expected = (BudgetExceeded if reason in ("pricing", "budget") else VisionUnsupported if reason == "vision"
                else GatewayError if reason == "context" else LlmError if reason == "key" else RoutingError)
    with pytest.raises(expected):
        if call_mode == "vision_text":
            gateway.call_vision_text("ask", packet, MESSAGES, [b"png"], profile=explicit, approved=approved)
        elif call_mode == "vision" or reason == "vision":
            gateway.call_vision("ask", packet, MESSAGES, SCHEMA, [b"png"], profile=explicit, approved=approved)
        else:
            gateway.call("ask", packet, MESSAGES, SCHEMA, profile=explicit, approved=approved)
    assert transport.calls == []


def test_capability_refusal_precedes_factory(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    def forbidden(*args: Any) -> Any:
        pytest.fail("factory accessed")
    with pytest.raises(VisionUnsupported):
        ModelGateway(conn, Config(), provider_factory=forbidden).call_vision(
            "asset_analyze", packet, MESSAGES, SCHEMA, [b"png"], profile="local")


@pytest.mark.parametrize("fields", [
    {"headers": {"aUtHoRiZaTiOn": "secret"}}, {"headers": {"Cookie": "secret"}},
    {"headers": {"X-Api-Key": "secret"}}, {"extra_body": {"model": "evil"}},
    {"extra_body": {"messages": []}}, {"extra_body": {"response_format": {}}},
    {"extra_body": {"max_tokens": 999}}, {"extra_body": {"api_key": "secret"}},
    {"extra_body": {"provider": {"token": "secret"}}},
])
def test_credentials_and_reserved_fields_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelProfileConfig(provider="openai_compatible", model="m", **fields)


def test_task_validation_and_yaml_reasoning() -> None:
    with pytest.raises(ValidationError, match="page_rewrite"):
        ModelsConfig(tasks={"typo": {"profile": "local"}})
    assert ModelsConfig(tasks={"ask": {"profile": "local", "reasoning": True}}).tasks["ask"].reasoning == "on"


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "none"])
@pytest.mark.parametrize("reasoning_param", ["none", "chat_template_kwargs", "openrouter", "reasoning_effort"])
def test_openai_request_modes(mode: str, reasoning_param: str, conn: sqlite3.Connection,
                              packet: EvidencePacket) -> None:
    cfg = Config()
    cfg.models.local = ModelProfileConfig(provider="openai_compatible", model="llama", structured_output=mode,
        reasoning="on", reasoning_param=reasoning_param, extra_body={"provider": {"data_collection": "deny"}},
        headers={"X-Title": "LitForge"})
    transport = Transport()
    client = OpenAICompatibleClient("http://local/v1", "llama", transport=transport)
    reply = ModelGateway(conn, cfg, {"local": client}).call("ask", packet, MESSAGES, SCHEMA)
    headers, body = transport.calls[0]
    assert "Authorization" not in headers and headers["X-Title"] == "LitForge"
    assert body["max_tokens"] == (8192 if reasoning_param == "chat_template_kwargs" else 4096)
    assert body["provider"] == {"data_collection": "deny"}
    if mode == "none":
        assert "response_format" not in body
    else:
        assert body["response_format"]["type"] == mode
    if mode != "json_schema":
        assert "schema" in body["messages"][0]["content"].lower()
    expected = {"chat_template_kwargs": {"chat_template_kwargs": {"enable_thinking": True}},
                "openrouter": {"reasoning": {"enabled": True}},
                "reasoning_effort": {"reasoning_effort": "medium"}, "none": {}}[reasoning_param]
    assert all(body[k] == v for k, v in expected.items())
    assert reply.content == {"ok": True}
    row = conn.execute("SELECT * FROM model_calls").fetchone()
    assert row["reasoning"] == "on" and "private thinking" not in str(dict(row))


@pytest.mark.parametrize("mode", ["on", "off", "auto"])
def test_ollama_reasoning(mode: str, conn: sqlite3.Connection, packet: EvidencePacket,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = []
    def call(self: Any, path: str, body: Any = None) -> Any:
        bodies.append(body)
        return {"message": {"content": '{"ok":true}', "thinking": "never store"}}
    monkeypatch.setattr(OllamaClient, "_call", call)
    cfg = Config()
    cfg.models.local.reasoning = mode
    ModelGateway(conn, cfg).call("ask", packet, MESSAGES, SCHEMA)
    assert ("think" not in bodies[0]) if mode == "auto" else bodies[0]["think"] == (mode == "on")


def test_retry_is_audited_and_guarded(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    transport = Transport("broken", '{"ok":true}')
    client = OpenAICompatibleClient("http://local/v1", "llama", transport=transport)
    ModelGateway(conn, Config(), {"local": client}).call("ask", packet, MESSAGES, SCHEMA)
    assert len(transport.calls) == 2
    assert "valid JSON" in str(transport.calls[1][1]["messages"])
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2


def test_context_and_evidence_trimming(conn: sqlite3.Connection) -> None:
    cfg = Config()
    cfg.models.local.context_tokens = 110
    packet = EvidencePacket(items=[EvidenceItem(id=f"e{i}", paper_id="p", text="x" * 80, page=1) for i in range(3)])
    gateway = ModelGateway(conn, cfg)
    fitted, messages = gateway.fit_evidence("ask", packet,
        lambda p: [{"role": "user", "content": "\n".join(i.text for i in p.items)}],
        max_output_tokens=50, schema=SCHEMA)
    assert len(fitted.items) == 1 and fitted.dropped_ids == ["e1", "e2"]
    run = start_run(conn, "ask", None, {}, {})
    transport = Transport()
    gateway.providers["local"] = OpenAICompatibleClient("http://local/v1", "m", transport=transport)
    gateway.call("ask", fitted, messages, SCHEMA, run_id=run, max_output_tokens=50)
    assert json.loads(conn.execute("SELECT dropped_evidence_ids_json FROM model_calls").fetchone()[0]) == ["e1", "e2"]
    assert json.loads(conn.execute("SELECT dropped_evidence_ids_json FROM analysis_runs").fetchone()[0]) == ["e1", "e2"]


def test_pricing_fixture_and_cache(tmp_path: Path) -> None:
    from litforge.llm.pricing import OpenRouterPricing
    calls = []
    def fetch() -> Any:
        calls.append(1)
        return json.loads((Path(__file__).parents[1] / "fixtures/openrouter_models.json").read_text())
    clock = [100000.0]
    pricing = OpenRouterPricing(tmp_path, fetch=fetch, clock=lambda: clock[0])
    assert pricing.lookup("openai/gpt-4").pricing == Pricing(input_per_million_usd=30, output_per_million_usd=60)
    assert pricing.lookup("unknown").pricing is None
    assert len(calls) == 1
    clock[0] += 86401
    assert pricing.lookup("openai/gpt-4").age_s == 0
    assert len(calls) == 2
    def fail() -> Any:
        raise OSError("secret")
    clock[0] += 86401
    failed = OpenRouterPricing(tmp_path, fetch=fail, clock=lambda: clock[0])
    assert failed.lookup("openai/gpt-4").pricing is None
    assert failed.lookup("openai/gpt-4").pricing is None


def test_models_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OllamaClient, "_call", lambda *a, **k: {"message": {"content": '{"ok":true}'}})
    runner = CliRunner()
    args = ["--data-dir", str(tmp_path / "data")]
    listed = runner.invoke(app, [*args, "models", "list"])
    assert listed.exit_code == 0 and "qwen3:4b" in listed.output and "local_vision" in listed.output
    tested = runner.invoke(app, [*args, "models", "test", "local"])
    assert tested.exit_code == 0 and "valid" in tested.output and "latency" in tested.output
    vision = runner.invoke(app, [*args, "models", "test", "local_vision", "--vision"])
    assert vision.exit_code == 0


def test_gateway_page_rewrite_and_approval(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    from litforge.parsing.vision import GatewayVision
    transport = Transport("# Transcribed page")
    cfg = Config()
    client = OpenAICompatibleClient("http://local/v1", "vision", transport=transport)
    engine = GatewayVision(ModelGateway(conn, cfg, {"local_vision": client}))
    assert engine.transcribe(b"png", "page text") == "# Transcribed page"
    assert transport.calls[0][1].get("response_format") is None
    row = conn.execute("SELECT task,prompt_version FROM model_calls").fetchone()
    assert row["task"] == "page_rewrite" and row["prompt_version"]
    assert engine.preview(3).image_description == "3 page images"
    cfg.models.profiles["cloud"] = ModelProfileConfig(provider="openai_compatible", model="v", remote=True,
        vision=True, pricing=Pricing(input_per_million_usd=0, output_per_million_usd=0))
    cfg.models.tasks = ModelsConfig(tasks={"page_rewrite": {"profile": "cloud"}}).tasks
    cfg.models.policy = "hybrid"
    cfg.privacy.allow_cloud = True
    gateway = ModelGateway(conn, cfg, {"cloud": client})
    with pytest.raises(RoutingError):
        GatewayVision(gateway).transcribe(b"page", "text")
    assert len(transport.calls) == 1


def test_doctor_props(monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.doctor import check_model_profile
    class Response:
        def __enter__(self) -> Any:
            return self
        def __exit__(self, *args: Any) -> None:
            pass
        def read(self, *args: Any) -> bytes:
            return b'{"modalities":{"vision":true},"default_generation_settings":{"n_ctx":8192}}'
    urls = []
    def open_url(url: str, **kwargs: Any) -> Response:
        urls.append(url)
        return Response()
    monkeypatch.setattr("litforge.doctor.open_url", open_url)
    cfg = ModelProfileConfig(provider="openai_compatible", model="llama", base_url="http://localhost:8080/v1",
                             reasoning_param="chat_template_kwargs", vision=False, context_tokens=16000)
    result = check_model_profile("llama", cfg)
    assert result.level == "warn" and "vision" in result.detail and "8192" in result.detail
    assert urls == ["http://localhost:8080/props"]


@pytest.mark.parametrize("bad", ["not-a-price", "NaN", "Infinity", "-1", None])
def test_bad_catalogue_prices_are_missing(tmp_path: Path, bad: Any) -> None:
    from litforge.llm.pricing import OpenRouterPricing
    pricing = OpenRouterPricing(tmp_path, fetch=lambda: {"data": [
        {"id": "m", "pricing": {"prompt": bad, "completion": "0"}}]})
    assert pricing.lookup("m").pricing is None


def test_pricing_fetch_failure_is_cached_without_writable_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm.pricing import OpenRouterPricing
    attempts = []
    def fail() -> Any:
        attempts.append(1)
        raise OSError("offline")
    def unwritable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("read only")
    monkeypatch.setattr(Path, "write_text", unwritable)
    pricing = OpenRouterPricing(tmp_path, fetch=fail)
    assert pricing.lookup("m").pricing is None
    assert pricing.lookup("m").pricing is None
    assert len(attempts) == 1


def test_keys_are_read_at_call_time_and_never_hashed_or_stored(
    conn: sqlite3.Connection, packet: EvidencePacket, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = Transport()
    client = OpenAICompatibleClient("http://local/v1", "model", "FAKE_PROVIDER_KEY", transport)
    cfg = Config()
    cfg.models.local.extra_body = {"provider": {"order": ["a", "b"]}}
    gateway = ModelGateway(conn, cfg, {"local": client})
    for key in ("first-secret", "second-secret"):
        monkeypatch.setenv("FAKE_PROVIDER_KEY", key)
        gateway.call("ask", packet, MESSAGES, SCHEMA)
    assert [h["Authorization"] for h, _ in transport.calls] == ["Bearer first-secret", "Bearer second-secret"]
    rows = [dict(r) for r in conn.execute("SELECT * FROM model_calls")]
    assert rows[0]["input_hash"] == rows[1]["input_hash"]
    assert all(v not in str(rows) for v in ("first-secret", "second-secret", "FAKE_PROVIDER_KEY", "order"))


def test_invalid_json_retry_rechecks_budget_and_accounts_tokens(
    conn: sqlite3.Connection, packet: EvidencePacket, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config()
    cfg.models.policy = "hybrid"
    cfg.privacy.allow_cloud = True
    cfg.budget.max_per_run_usd = 0.00006
    cfg.models.profiles["paid"] = ModelProfileConfig(provider="openai_compatible", model="m", remote=True,
        api_key_env="FAKE_PROVIDER_KEY", pricing=Pricing(input_per_million_usd=1, output_per_million_usd=2))
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "fake")
    transport = Transport("bad JSON")
    client = OpenAICompatibleClient("https://fake.invalid/v1", "m", "FAKE_PROVIDER_KEY", transport)
    gateway = ModelGateway(conn, cfg, {"paid": client})
    run = start_run(conn, "ask", None, {}, {})
    with pytest.raises(BudgetExceeded):
        gateway.call("ask", packet, MESSAGES, SCHEMA, "paid", run_id=run, max_output_tokens=1)
    assert len(transport.calls) == 1
    record = conn.execute("SELECT * FROM model_calls").fetchone()
    assert record["cost_usd"] == pytest.approx(0.000013)
    assert record["input_tokens"] == 3 and record["output_tokens"] == 5


def test_retry_refuses_expanded_context_before_second_transport(conn: sqlite3.Connection,
                                                               packet: EvidencePacket) -> None:
    cfg = Config()
    cfg.models.local.context_tokens = 40
    transport = Transport("bad JSON")
    client = OpenAICompatibleClient("http://local/v1", "m", transport=transport)
    with pytest.raises(GatewayError, match="context of 40"):
        ModelGateway(conn, cfg, {"local": client}).call("ask", packet, MESSAGES, SCHEMA, max_output_tokens=1)
    assert len(transport.calls) == 1


def test_output_profile_default_and_override(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    cfg = Config()
    cfg.models.local.max_output_tokens = 123
    gateway = ModelGateway(conn, cfg)
    assert gateway.preview("ask", packet, MESSAGES).max_output_tokens == 123
    assert gateway.preview("ask", packet, MESSAGES, max_output_tokens=42).max_output_tokens == 42


def test_provider_extra_body_preserves_nonreserved_options(conn: sqlite3.Connection, packet: EvidencePacket,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config()
    cfg.models.local.extra_body = {"options": {"top_k": 20}}
    bodies = []
    def call(*args: Any, **kwargs: Any) -> Any:
        bodies.append(args[2])
        return {"message": {"content": '{"ok":true}'}}
    monkeypatch.setattr(OllamaClient, "_call", call)
    ModelGateway(conn, cfg).call("ask", packet, MESSAGES, SCHEMA)
    assert bodies[0]["options"]["top_k"] == 20


def test_context_includes_schema_before_transport(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    cfg = Config()
    cfg.models.local.context_tokens = 50
    transport = Transport()
    client = OpenAICompatibleClient("http://local/v1", "m", transport=transport)
    schema = {"type": "object", "description": "long schema " * 100}
    with pytest.raises(GatewayError, match="context of 50"):
        ModelGateway(conn, cfg, {"local": client}).call("ask", packet, MESSAGES, schema, max_output_tokens=1)
    assert transport.calls == []


def test_ollama_headers_are_forwarded(conn: sqlite3.Connection, packet: EvidencePacket,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config()
    cfg.models.local.headers = {"X-Title": "LitForge"}
    captured = []
    def call(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return {"message": {"content": '{"ok":true}'}}
    monkeypatch.setattr(OllamaClient, "_call", call)
    ModelGateway(conn, cfg).call("ask", packet, MESSAGES, SCHEMA)
    assert captured[0]["headers"] == {"X-Title": "LitForge"}


def test_unsupported_vision_does_not_even_fetch_prices(conn: sqlite3.Connection, packet: EvidencePacket,
                                                      tmp_path: Path) -> None:
    from litforge.llm.pricing import OpenRouterPricing
    fetched = []
    def fetch() -> Any:
        fetched.append(1)
        return {"data": []}
    cfg = Config()
    cfg.models.policy = "hybrid"
    cfg.privacy.allow_cloud = True
    cfg.models.profiles["paid"] = ModelProfileConfig(provider="openai_compatible", model="m", remote=True,
        pricing_source="openrouter")
    gateway = ModelGateway(conn, cfg, pricing=OpenRouterPricing(tmp_path, fetch=fetch))
    with pytest.raises(VisionUnsupported):
        gateway.call_vision("asset_analyze", packet, MESSAGES, SCHEMA, [b"png"], "paid", approved=True)
    assert fetched == []


def test_plain_text_provider_reply_normalization(conn: sqlite3.Connection, packet: EvidencePacket) -> None:
    class TextProvider:
        def chat_json(self, messages: Any, schema: Any) -> dict[str, Any]:
            return {}
        def vision_text(self, messages: Any, images: Any) -> str:
            return "# Page"
    reply = ModelGateway(conn, Config(), {"local_vision": TextProvider()}).call_vision_text(
        "page_rewrite", packet, MESSAGES, [b"png"])
    assert reply.content == "# Page" and reply.provider == "ollama"


@pytest.mark.parametrize("policy,allow,approve,success", [
    ("local_only", True, True, False), ("ask_before_cloud", False, True, False),
    ("ask_before_cloud", True, False, False), ("hybrid", True, False, False), ("hybrid", True, True, True),
])
def test_remote_model_diagnostics_require_permission_and_approval(
    tmp_path: Path, policy: str, allow: bool, approve: bool, success: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    from litforge.llm.openai_compatible import UrllibTransport
    captured = []
    def request(*args: Any, **kwargs: Any) -> Any:
        captured.append(args[3])
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}
    monkeypatch.setattr(UrllibTransport, "request", request)
    monkeypatch.setenv("FAKE_DIAGNOSTIC_KEY", "secret-not-in-output")
    config = tmp_path / "models.yaml"
    config.write_text(yaml.safe_dump({"privacy": {"allow_cloud": allow}, "models": {"policy": policy,
        "profiles": {"paid": {"provider": "openai_compatible", "model": "v", "remote": True, "vision": True,
            "base_url": "https://fake.invalid/v1", "api_key_env": "FAKE_DIAGNOSTIC_KEY",
            "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 2}}}}}))
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path / "data"), "--config", str(config),
        "models", "test", "paid", "--vision", *(["--approve"] if approve else [])])
    assert (result.exit_code == 0) == success
    assert len(captured) == int(success)
    assert "secret-not-in-output" not in result.output
    if success:
        import struct
        from base64 import b64decode
        url = captured[0]["messages"][0]["content"][1]["image_url"]["url"]
        assert struct.unpack(">II", b64decode(url.split(",")[1])[16:24]) == (64, 64)


def test_example_config_and_defaults() -> None:
    from litforge.config import REPO_ROOT, load_config
    cfg = load_config(REPO_ROOT / "configs/models.example.yaml")
    assert cfg.models.profiles["llamacpp_local"].api_key_env is None
    assert all(cfg.models.profiles[name].remote for name in (
        "openrouter_text", "deepseek", "dashscope_qwen", "moonshot_kimi", "openai",
        "anthropic_openai_compat", "gemini_openai_compat", "university_vllm"))
    default = load_config()
    assert default.models.policy == "local_only" and not default.privacy.allow_cloud
    assert default.models.local.model == "qwen3:4b" and default.models.local.provider == "ollama"
    assert default.models.tasks == {}


def test_ask_trims_ranked_evidence_and_rejects_dropped_citations(conn: sqlite3.Connection) -> None:
    from lens_fakes import seed

    from litforge.analysis.ask import ask, plan_calls
    seed(conn, paper="one", text="Results: gain 3.25. " + "alpha " * 150)
    seed(conn, paper="two", text="Results: gain 3.25. " + "beta " * 150)
    cfg = Config()
    gateway = ModelGateway(conn, cfg)
    original = plan_calls(conn, gateway, "gain")[0]
    assert len(original.evidence_ids) == 2
    cfg.models.local.context_tokens = original.estimated_input_tokens + original.max_output_tokens - 1
    preview = plan_calls(conn, gateway, "gain")[0]
    assert len(preview.evidence_ids) == len(preview.dropped_evidence_ids) == 1
    dropped = preview.dropped_evidence_ids[0]
    transport = Transport(json.dumps({"sentences": [
        {"text": "A claim.", "kind": "reported_fact", "evidence_ids": [dropped]}], "insufficient": False}))
    gateway.providers["local"] = OpenAICompatibleClient("http://local/v1", "m", transport=transport)
    result = ask(conn, gateway, "gain")
    assert result.sentences[0].kind == "unsupported" and result.invalid_citations == [dropped]
    assert dropped not in json.dumps(transport.calls[0][1])
    row = conn.execute("SELECT dropped_evidence_ids_json FROM analysis_runs WHERE id=?", (result.run_id,)).fetchone()
    assert json.loads(row[0]) == [dropped]


@pytest.mark.parametrize("path", ["chat_json", "vision_json", "vision_text"])
@pytest.mark.parametrize("context", [None, 24576])
def test_ollama_context_request_paths(path: str, context: int | None, conn: sqlite3.Connection,
                                      packet: EvidencePacket, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = []
    def call(*args: Any, **kwargs: Any) -> Any:
        bodies.append(args[2])
        return {"message": {"content": '# Page' if path == "vision_text" else '{"ok":true}'}}
    monkeypatch.setattr(OllamaClient, "_call", call)
    cfg = Config()
    cfg.models.local.vision = True
    cfg.models.local.context_tokens = context
    gateway = ModelGateway(conn, cfg)
    # Roughly 9k input tokens, before output and image allowance.
    messages = [{"role": "user", "content": "x" * 36000}]
    if path == "chat_json":
        gateway.call("ask", packet, messages, SCHEMA, max_output_tokens=512)
    elif path == "vision_json":
        gateway.call_vision("asset_analyze", packet, messages, SCHEMA, [b"png"], "local", max_output_tokens=512)
    else:
        gateway.call_vision_text("page_rewrite", packet, messages, [b"png"], "local", max_output_tokens=512)
    assert bodies[0]["options"]["num_ctx"] == (context or 16384)
    assert bodies[0]["options"]["num_predict"] == 512


def test_ollama_page_rewrite_context(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.parsing.vision import GatewayVision
    bodies = []
    def call(*args: Any, **kwargs: Any) -> Any:
        bodies.append(args[2])
        return {"message": {"content": "# Page"}}
    monkeypatch.setattr(OllamaClient, "_call", call)
    gateway = ModelGateway(conn, Config())
    assert gateway.resolve("ask")[1].context_tokens is None
    assert gateway.resolve("page_rewrite", vision=True)[1].context_tokens is None
    assert GatewayVision(gateway).transcribe(b"png", "") == "# Page"
    assert bodies[0]["options"]["num_ctx"] >= 8192
    assert bodies[0]["options"]["num_predict"] == 8192


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("mode", ["json_schema", "json_object", "none"])
@pytest.mark.parametrize("bad", [{}, {"ok": "true"}, {"ok": 1}, []])
@pytest.mark.parametrize("recover", [False, True])
def test_schema_invalid_reply_retries(provider: str, vision: bool, mode: str, bad: Any, recover: bool,
                                      conn: sqlite3.Connection, packet: EvidencePacket,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    contents = [json.dumps(bad), '{"ok":true}' if recover else json.dumps(bad)]
    transport = Transport(*contents)
    bodies = []
    def call(*args: Any, **kwargs: Any) -> Any:
        bodies.append(args[2])
        return {"message": {"content": contents[len(bodies)-1]}, "prompt_eval_count": 3, "eval_count": 5}
    monkeypatch.setattr(OllamaClient, "_call", call)
    client = (OllamaClient("http://local", "m") if provider == "ollama" else
              OpenAICompatibleClient("http://local/v1", "m", transport=transport))
    cfg = Config()
    cfg.models.local.vision = True
    cfg.models.local.structured_output = mode
    gateway = ModelGateway(conn, cfg, {"local": client})
    def request() -> Any:
        if vision:
            return gateway.call_vision("asset_analyze", packet, MESSAGES, SCHEMA, [b"png"], "local")
        return gateway.call("ask", packet, MESSAGES, SCHEMA)
    if recover:
        assert request().content == {"ok": True}
    else:
        with pytest.raises(LlmError, match="^invalid JSON response from provider$"):
            request()
    requests = bodies if provider == "ollama" else [body for _, body in transport.calls]
    assert len(requests) == 2
    assert "valid JSON" in str(requests[1]["messages"])
    rows = conn.execute("SELECT status,input_tokens,output_tokens FROM model_calls").fetchall()
    assert [(r["status"], r["input_tokens"], r["output_tokens"]) for r in rows] == [
        ("error", 3, 5), ("ok" if recover else "error", 3, 5)]


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("kind,valid,invalid", [
    ("string", "text", 7), ("boolean", True, 1), ("integer", 3, True),
    ("number", 1.5, "1.5"), ("array", [], {}), ("object", {}, []), ("null", None, "null"),
])
def test_schema_top_level_types_and_local_refs(provider: str, kind: str, valid: Any, invalid: Any,
                                               conn: sqlite3.Connection, packet: EvidencePacket,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"$ref": "#/$defs/Reply", "$defs": {
        "Reply": {"type": "object", "properties": {"value": {"$ref": "#/$defs/Value"}}, "required": ["value"]},
        "Value": {"type": kind}}}
    contents = [json.dumps({"value": invalid}), json.dumps({"value": valid})]
    transport = Transport(*contents)
    bodies = []
    def call(*args: Any, **kwargs: Any) -> Any:
        bodies.append(args[2])
        return {"message": {"content": contents[len(bodies)-1]}}
    monkeypatch.setattr(OllamaClient, "_call", call)
    client = (OllamaClient("http://local", "m") if provider == "ollama" else
              OpenAICompatibleClient("http://local/v1", "m", transport=transport))
    reply = ModelGateway(conn, Config(), {"local": client}).call("ask", packet, MESSAGES, schema)
    assert reply.content == {"value": valid}
    assert len(bodies if provider == "ollama" else transport.calls) == 2


@pytest.mark.parametrize("name", ["X-Auth-Token", "X-Access-Token", "X-Goog-Api-Key", "Api-Token",
    "Custom_AUTH", "clientSecret", "db_password", "Set-Cookie", "myCredential", "Session-ID"])
@pytest.mark.parametrize("location", ["headers", "extra_body", "nested"])
def test_credential_name_fragments_rejected(name: str, location: str) -> None:
    fields = {location: {name: "secret"}} if location != "nested" else {
        "extra_body": {"provider": [{name: "secret"}]}}
    with pytest.raises(ValidationError, match="credentials belong in api_key_env"):
        ModelProfileConfig(provider="openai_compatible", model="m", **fields)


@pytest.mark.parametrize("name", ["X-Title", "HTTP-Referer", "x-title", "http-referer", "provider", "temperature"])
@pytest.mark.parametrize("location", ["headers", "extra_body"])
def test_noncredential_options_allowed(name: str, location: str) -> None:
    ModelProfileConfig(provider="openai_compatible", model="m", **{location: {name: "value"}})
