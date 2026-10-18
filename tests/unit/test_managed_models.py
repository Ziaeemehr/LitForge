from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from litforge.cli import app
from litforge.config import Config, LlamaCppServerConfig, load_config
from litforge.llm.hardware import Hardware, detect_hardware
from litforge.llm.model_setup import PROVIDERS, api_model_patch, config_change, setup_proposal


def test_config_merge_and_partial_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.yaml").write_text("discovery: {limit: 17}\nmodels: {policy: hybrid}\ncustom: keep\n")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("discovery: {limit: 21}\n")
    monkeypatch.setenv("LITFORGE_CONFIG", str(explicit))
    assert load_config(data_root=tmp_path).discovery.limit == 21
    change = config_change(tmp_path, {"vision": {"model": None}})
    assert "custom: keep" in change.diff and "config.yaml" in change.diff
    assert not (tmp_path / "state").exists()
    change.apply()
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["custom"] == "keep"
    assert load_config(data_root=tmp_path).models.policy == "hybrid"
    assert load_config(data_root=tmp_path).vision.model is None
    assert not list(tmp_path.glob("*.tmp"))


def test_stale_diff_refused(tmp_path: Path) -> None:
    change = config_change(tmp_path, {"discovery": {"limit": 1}})
    (tmp_path / "config.yaml").write_text("discovery: {limit: 2}\n")
    with pytest.raises(ValueError, match="changed"):
        change.apply()
    assert load_config(data_root=tmp_path).discovery.limit == 2


@pytest.mark.parametrize("host", ["0.0.0.0", "example.com", "192.168.1.1", "localhost.evil"])
def test_loopback_only(host: str) -> None:
    with pytest.raises(ValidationError):
        LlamaCppServerConfig(host=host)


def hardware(ram: int, apple: bool = False, vram: int = 0, **tools: Any) -> Hardware:
    return Hardware(os="Darwin" if apple else "Linux", arch="arm64" if apple else "x86_64",
                    ram_gb=ram, unified_memory=apple, gpu_vram_gb=vram, **tools)


@pytest.mark.parametrize("hw,tier", [
    (hardware(16, True), "medium"), (hardware(24, True), "large"), (hardware(36, True), "large"),
    (hardware(32, vram=8), "medium"), (hardware(32, vram=16), "large"),
    (hardware(64, vram=24), "large"), (hardware(8), "small"), (hardware(32), "small"),
])
def test_tiers(tmp_path: Path, hw: Hardware, tier: str) -> None:
    hw.llama_server = "/fake/llama-server"
    hw.ollama = "/fake/ollama"
    proposal = setup_proposal(tmp_path, hw)
    assert proposal.tier == ("cloud" if tier == "small" else tier)
    assert proposal.download_gb > 0 and proposal.memory_gb > 0
    assert not (tmp_path / "config.yaml").exists()


def test_fallback_and_preset(tmp_path: Path) -> None:
    hw = hardware(36, True, ollama="/fake/ollama")
    proposal = setup_proposal(tmp_path, hw)
    assert proposal.tier == "medium"
    assert "brew install llama.cpp" in proposal.notes
    hw.llama_server = "/fake/llama-server"
    proposal = setup_proposal(tmp_path, hw)
    assert "ubatch-size = 1152" in proposal.preset
    assert "batch-size = 2048" in proposal.preset
    assert "[gemma4]" in proposal.preset and "[qwen3-4b]" in proposal.preset
    proposal.change.apply()
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.profiles["gemma4"].base_url == "http://127.0.0.1:8093/v1"
    assert cfg.models.tasks["screen_fulltext"].profile == "qwen3-4b"
    assert cfg.models.tasks["page_rewrite"].profile == "gemma4"
    assert (tmp_path / "llamacpp-models.ini").read_text() == proposal.preset


def test_detection_injected_and_optional(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    def run(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "sysctl":
            return str(24 * 1024**3)
        if args[0] == "nvidia-smi":
            return "Fake 8, 8192\nFake 24, 24576"
        return "version 11429"
    hw = detect_hardware(tmp_path, runner=run, which=lambda name: "/fake/" + name,
                         system="Darwin", arch="arm64")
    assert hw.ram_gb == 24 and hw.gpu_vram_gb == 24
    assert hw.unified_memory and hw.llama_version == "version 11429"
    def fail(args: list[str]) -> str:
        raise OSError("optional probe failed")
    assert detect_hardware(tmp_path, runner=fail, which=lambda name: None,
                           system="Darwin", arch="arm64").ram_gb == 0


@pytest.mark.parametrize("provider", list(PROVIDERS))
def test_api_providers_no_secrets(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROVIDERS[provider][1], "SECRET_VALUE")
    patch = api_model_patch(provider, "user-model", name="api", tasks=["ask"], vision=True, reasoning="on")
    text = json.dumps(patch)
    assert "SECRET_VALUE" not in text
    cfg = Config.model_validate(patch)
    profile = cfg.models.profiles["api"]
    assert profile.remote and profile.vision and profile.reasoning == "on"
    assert profile.api_key_env == PROVIDERS[provider][1]
    assert profile.pricing is None
    assert not cfg.privacy.allow_cloud
    assert cfg.models.tasks["ask"].profile == "api"
    priced = api_model_patch(provider, "m", price_in=1, price_out=2)
    assert next(iter(priced["models"]["profiles"].values()))["pricing"]["output_per_million_usd"] == 2


def test_cli_cloud_requires_confirmation_and_add_preserves(tmp_path: Path) -> None:
    runner = CliRunner()
    args = ["--data-dir", str(tmp_path), "models"]
    result = runner.invoke(app, [*args, "cloud", "on"], input="n\n")
    assert result.exit_code == 0 and not (tmp_path / "config.yaml").exists()
    result = runner.invoke(app, [*args, "cloud", "on"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "evidence" in result.output.lower()
    cfg = load_config(data_root=tmp_path)
    assert cfg.privacy.allow_cloud and cfg.models.policy == "ask_before_cloud"
    result = runner.invoke(app, [*args, "add", "openai", "--model", "supplied", "--name", "api",
                                 "--task", "ask"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "pricing missing" in result.output
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.profiles["api"].model == "supplied" and cfg.privacy.allow_cloud
    result = runner.invoke(app, [*args, "remove", "api"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "ask" not in load_config(data_root=tmp_path).models.tasks


def test_medium_small_transition_and_reserved_server_links(tmp_path: Path) -> None:
    hw = hardware(36, True, llama_server="/fake/llama-server", ollama="/fake/ollama")
    setup_proposal(tmp_path, hw, "large").change.apply()
    setup_proposal(tmp_path, hw, "small").change.apply()
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.local.provider == "ollama" and cfg.models.local.server is None
    assert cfg.vision.model is None
    with pytest.raises(ValidationError):
        Config(models={"servers": {"llamacpp": {}}, "profiles": {"bad": {
            "provider": "openai_compatible", "model": "bad", "server": "llamacpp", "remote": True}}})


def test_config_atomic_failure_keeps_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import files
    original = "custom: keep\n"
    (tmp_path / "config.yaml").write_text(original)
    change = config_change(tmp_path, {"privacy": {"allow_cloud": True}})
    def fail(*args: Any) -> None:
        raise OSError("rename failed")
    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError):
        change.apply()
    assert (tmp_path / "config.yaml").read_text() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_setup_ollama_downloads_on_first_guarded_use(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.analysis.gateway import ModelGateway
    from litforge.llm.evidence import EvidenceItem, EvidencePacket
    from litforge.screening.llm import OllamaClient
    calls: list[str] = []
    def request(self: Any, path: str, body: Any = None, **kwargs: Any) -> Any:
        calls.append(path)
        if path == "/api/tags":
            return {"models": []}
        if path == "/api/pull":
            assert body == {"name": "qwen3:4b", "stream": False}
            return {"status": "success"}
        return {"message": {"content": "{}"}}
    monkeypatch.setattr(OllamaClient, "_call", request)
    cfg = Config(models={"setup_tier": "small"})
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")])
    gateway = ModelGateway(conn, cfg)
    assert "download" in gateway.preview("ask", packet, []).model_notice
    assert calls == []
    gateway.call("ask", packet, [], {})
    assert calls == ["/api/tags", "/api/pull", "/api/chat"]


def test_readding_api_profile_replaces_owned_fields(tmp_path: Path) -> None:
    from litforge.config import Paths
    from litforge.ui.api import ModelsPanel
    panel = ModelsPanel(Paths(tmp_path), Config())
    first = panel.preview({"action": "add", "provider": "openrouter", "model": "m", "name": "api"})
    panel.apply(first["token"], True)
    second = panel.preview({"action": "add", "provider": "openai", "model": "m2", "name": "api"})
    panel.apply(second["token"], True)
    cfg = panel.config.models.profiles["api"]
    assert cfg.pricing_source == "config" and cfg.extra_body == {} and cfg.headers == {}


def test_doctor_router_uses_advertised_modalities(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from litforge.doctor import check_model_profile
    profile = Config(models={"servers": {"llamacpp": {}}, "local": {
        "provider": "openai_compatible", "server": "llamacpp", "model": "gemma4", "vision": True}}).models.local
    def open_url(url: str, **kwargs: Any) -> Any:
        assert url.endswith("/v1/models")
        return io.BytesIO(json.dumps({"data": [{"id": "gemma4", "status": {"value": "unloaded"},
                                                "architecture": {"input_modalities": ["text"]}}]}).encode())
    monkeypatch.setattr("litforge.doctor.open_url", open_url)
    check = check_model_profile("gemma4", profile)
    assert check.level == "warn" and "vision=False" in check.detail


def test_cloud_diagnostic_refuses_disabled_cloud_before_any_provider(conn: Any) -> None:
    from litforge.analysis.gateway import ModelGateway
    from litforge.llm.commands import diagnostic_call
    cfg = Config.model_validate(api_model_patch("openai", "supplied", name="api", price_in=1, price_out=2))
    cfg.models.policy = "hybrid"
    def forbidden(*args: Any) -> Any:
        pytest.fail("provider reached despite missing cloud permission")
    with pytest.raises(ValueError, match="cloud"):
        diagnostic_call(ModelGateway(conn, cfg, provider_factory=forbidden), "api", approved=True)


def test_api_keys_are_never_persisted_or_hashed(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.analysis.gateway import ModelGateway
    from litforge.llm.evidence import EvidenceItem, EvidencePacket
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    cfg = Config.model_validate(api_model_patch("openai", "supplied", name="api", price_in=1, price_out=2))
    cfg.models.policy = "ask_before_cloud"
    cfg.privacy.allow_cloud = True
    seen: list[str] = []
    class Transport:
        def request(self, url: str, headers: Any, body: Any, timeout_s: float) -> Any:
            seen.append(headers["Authorization"])
            return {"choices": [{"message": {"content": "{}"}}]}
    gateway = ModelGateway(conn, cfg, {"api": OpenAICompatibleClient("https://fake.invalid", "supplied",
                                                                  "OPENAI_API_KEY", transport=Transport())})
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")])
    for key in ("FIRST_SECRET_VALUE", "SECOND_SECRET_VALUE"):
        monkeypatch.setenv("OPENAI_API_KEY", key)
        gateway.call("ask", packet, [], {}, "api", approved=True)
    rows = conn.execute("SELECT * FROM model_calls").fetchall()
    assert rows[0]["input_hash"] == rows[1]["input_hash"]
    assert "SECRET_VALUE" not in str([dict(row) for row in rows])
    assert seen == ["Bearer FIRST_SECRET_VALUE", "Bearer SECOND_SECRET_VALUE"]


def test_nonsetup_provider_keeps_custom_timeout(conn: Any) -> None:
    from litforge.analysis.gateway import ModelGateway
    from litforge.llm.evidence import EvidenceItem, EvidencePacket
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    timeouts: list[float] = []
    class Transport:
        def request(self, url: str, headers: Any, body: Any, timeout_s: float) -> Any:
            timeouts.append(timeout_s)
            return {"choices": [{"message": {"content": "{}"}}]}
    client = OpenAICompatibleClient("http://fake", "m", transport=Transport(), timeout_s=42)
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")])
    ModelGateway(conn, Config(), {"local": client}).call("ask", packet, [], {})
    assert timeouts == [42]


def test_cli_setup_yes_and_server_controls_use_selected_data_root(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import commands
    hw = hardware(24, True, llama_server="/fake/llama-server")
    monkeypatch.setattr(commands, "detect_hardware", lambda root: hw)
    runner, args = CliRunner(), ["--data-dir", str(tmp_path), "models"]
    result = runner.invoke(app, [*args, "setup"], input="n\n")
    assert result.exit_code == 0 and not (tmp_path / "config.yaml").exists()
    result = runner.invoke(app, [*args, "setup", "--tier", "large", "--yes"])
    assert result.exit_code == 0, result.output
    assert str(tmp_path / "llamacpp-models.ini") in result.output
    calls: list[str] = []
    class Server:
        def ensure_running(self, cfg: Any, model: str) -> bool:
            calls.append("start:" + model)
            return False
        def stop(self, cfg: Any) -> bool:
            calls.append("stop")
            return True
        def status(self, cfg: Any) -> Any:
            return {"running": True, "preset_models": ["qwen3-4b"],
                    "models": [{"id": "qwen3-4b", "status": "loaded"}]}
    monkeypatch.setattr(commands, "LlamaCppServer", lambda root: Server())
    assert runner.invoke(app, [*args, "start"]).exit_code == 0
    assert "loaded" in runner.invoke(app, [*args, "status"]).output
    assert runner.invoke(app, [*args, "stop"]).exit_code == 0
    assert calls == ["start:qwen3-4b", "stop"]
