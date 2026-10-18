"""Regressions from the first managed-router end-to-end run."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from litforge.analysis.gateway import ModelGateway
from litforge.cli import app
from litforge.config import Config, LlamaCppServerConfig, ModelProfileConfig
from litforge.llm.hardware import Hardware
from litforge.llm.model_setup import setup_proposal


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "localhost"])
@pytest.mark.parametrize("client", ["openai", "ollama", "probe", "doctor"])
def test_loopback_ignores_proxy(monkeypatch: pytest.MonkeyPatch, host: str, client: str) -> None:
    import urllib.request

    from litforge.doctor import check_model_profile
    from litforge.llm.llamacpp_server import http_probe
    from litforge.llm.openai_compatible import UrllibTransport
    from litforge.screening.llm import OllamaClient

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("NO_PROXY", "")
    seen = []
    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            return io.BytesIO(b'{"data": [{"id": "m"}]}')
    def build(*handlers: Any) -> Any:
        assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        seen.append(True)
        return Opener()
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("loopback request used the proxy-aware global opener")
    monkeypatch.setattr(urllib.request, "build_opener", build)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    url = f"http://{host}:8093/v1"
    if client == "openai":
        UrllibTransport().request(url, {}, {}, 120)
    elif client == "ollama":
        OllamaClient(url, "m")._call("/api/tags")
    elif client == "probe":
        http_probe(url + "/models")
    else:
        check_model_profile("m", ModelProfileConfig(provider="openai_compatible", model="m",
                                                    server="llamacpp", base_url=url))
    assert seen


def test_remote_keeps_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    from litforge.llm.openai_compatible import UrllibTransport
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8888")
    calls = []
    def open_url(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return io.BytesIO(b'{}')
    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    UrllibTransport().request("https://provider.example/v1", {}, {}, 120)
    assert calls


def test_large_setup_routes_and_defaults(tmp_path: Path, conn: Any) -> None:
    hw = Hardware(os="Darwin", arch="arm64", ram_gb=36, unified_memory=True, llama_server="llama-server")
    (tmp_path / "config.yaml").write_text("vision: {model: existing-ollama-vision}\n")
    proposal = setup_proposal(tmp_path, hw, "large")
    proposal.change.apply()
    from litforge.config import load_config
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.tasks["screen"].profile == "qwen3-4b"
    assert cfg.models.vision_profile == "gemma4"
    assert cfg.vision.model == "existing-ollama-vision"
    assert all(task.reasoning == "off" for task in cfg.models.tasks.values())
    assert cfg.models.servers["llamacpp"].port == 8093
    for profile in [cfg.models.local, *cfg.models.profiles.values()]:
        assert profile.timeout_s == 600
        assert profile.reasoning_output_tokens == 8192
    gateway = ModelGateway(conn, cfg)
    assert gateway.resolve("screen")[1].model == "qwen3-4b"
    assert gateway._default_provider("gemma4", cfg.models.profiles["gemma4"]).timeout_s == 600


def test_vision_default_resolution_and_preview(conn: Any) -> None:
    from litforge.llm.evidence import EvidenceItem, EvidencePacket
    cfg = Config(models={"vision_profile": "images", "profiles": {
        "images": {"provider": "ollama", "model": "images", "vision": True},
        "mapped": {"provider": "ollama", "model": "mapped", "vision": True}},
        "tasks": {"page_rewrite": {"profile": "mapped"}}})
    gateway = ModelGateway(conn, cfg)
    assert gateway.resolve("asset_analyze", vision=True)[0] == "images"
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")])
    assert gateway.preview_vision("asset_analyze", packet, []).profile == "images"
    assert gateway.resolve("page_rewrite", vision=True)[0] == "mapped"
    assert gateway.resolve("page_rewrite", "local_vision", True)[0] == "local_vision"


def test_llamacpp_reasoning_default_and_explicit_override() -> None:
    cfg = ModelProfileConfig(provider="openai_compatible", model="m", server="llamacpp")
    assert cfg.reasoning_output_tokens == 8192
    assert ModelProfileConfig(provider="ollama", model="m").reasoning_output_tokens == 4096
    assert ModelProfileConfig(provider="openai_compatible", model="m", server="llamacpp",
                              reasoning_output_tokens=1234).reasoning_output_tokens == 1234
    assert cfg.timeout_s == 120
    assert LlamaCppServerConfig().port == 8093


def test_setup_readable_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import commands
    hw = Hardware(os="Darwin", arch="arm64", ram_gb=36, unified_memory=True, llama_server="llama-server")
    monkeypatch.setattr(commands, "detect_hardware", lambda root: hw)
    args = ["--data-dir", str(tmp_path), "models", "setup"]
    result = CliRunner().invoke(app, args, input="n\n")
    assert result.exit_code == 0, result.output
    assert "Hardware:" in result.output and "Tier: large" in result.output
    assert "gemma4" in result.output and "GB" in result.output
    assert result.output.count("--- " + str(tmp_path / "config.yaml")) == 1
    assert '"hardware"' not in result.output
    result = CliRunner().invoke(app, [*args, "--json"], input="n\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["hardware"]["ram_gb"] == 36


@pytest.mark.parametrize("case", ["vision", "reasoning", "timeout", "port"])
def test_large_proposal_regressions(tmp_path: Path, case: str) -> None:
    import yaml
    hw = Hardware(os="Darwin", arch="arm64", ram_gb=36, unified_memory=True, llama_server="llama-server")
    proposal = setup_proposal(tmp_path, hw, "large")
    data = yaml.safe_load(proposal.change.after[tmp_path / "config.yaml"])
    models = data["models"]
    if case == "vision":
        assert models.get("vision_profile") == "gemma4" and "vision" not in data
    elif case == "reasoning":
        assert all(t["reasoning"] == "off" for t in models["tasks"].values())
    elif case == "timeout":
        assert all(p.get("timeout_s") == 600 for p in models["profiles"].values())
    else:
        assert models["servers"]["llamacpp"]["port"] == 8093


def test_status_and_list_readable_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import commands
    (tmp_path / "config.yaml").write_text('models:\n  servers: {llamacpp: {}}\n  tasks: {ask: {profile: local}}\n')
    class Server:
        def status(self, cfg: Any) -> Any:
            return {"running": False, "foreign": True, "owned": False,
                    "models": [], "base_url": cfg.base_url}
    monkeypatch.setattr(commands, "LlamaCppServer", lambda root: Server())
    args = ["--data-dir", str(tmp_path), "models"]
    runner = CliRunner()
    result = runner.invoke(app, [*args, "status"])
    assert result.exit_code == 0, result.output
    assert "foreign server" in result.output.lower() and "8093" in result.output
    result = runner.invoke(app, [*args, "status", "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["foreign"]
    result = runner.invoke(app, [*args, "list"])
    assert result.exit_code == 0 and "Task" in result.output and '"ask"' not in result.output
    result = runner.invoke(app, [*args, "list", "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["tasks"]["ask"]["profile"] == "local"


def test_doctor_identifies_foreign_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge import doctor
    monkeypatch.setattr(doctor, "open_url", lambda *a, **kw: io.BytesIO(b'{"data": []}'))
    profile = Config(models={"servers": {"llamacpp": {}}, "local": {
        "provider": "openai_compatible", "server": "llamacpp", "model": "m"}}).models.local
    result = doctor.check_model_profile("local", profile)
    assert "foreign server" in result.detail.lower() and "8093" in result.detail


def test_status_foreign_catalogue(tmp_path: Path) -> None:
    from litforge.llm.llamacpp_server import LlamaCppServer
    (tmp_path / "llamacpp-models.ini").write_text("[gemma4]\nhf-repo = test\n")
    def probe(url: str) -> Any:
        return {"status": "ok"} if url.endswith("/health") else {"data": [{"id": "users-model"}]}
    server = LlamaCppServer(tmp_path, probe=probe)
    assert server.status(LlamaCppServerConfig())["foreign"]


def test_profile_timeout_applies_to_retries(conn: Any) -> None:
    from litforge.llm.evidence import EvidenceItem, EvidencePacket
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    cfg = Config(models={"servers": {"llamacpp": {"managed": True}}, "local": {
        "provider": "openai_compatible", "model": "m", "server": "llamacpp", "timeout_s": 600}})
    timeouts = []
    class Transport:
        def request(self, url: str, headers: Any, body: Any, timeout_s: float) -> Any:
            timeouts.append(timeout_s)
            return {"choices": [{"message": {"content": "bad" if len(timeouts) == 1 else "{}"}}]}
    gateway = ModelGateway(conn, cfg, ensure_server=lambda *args: len(timeouts) == 0)
    client = gateway._default_provider("local", cfg.models.local)
    assert isinstance(client, OpenAICompatibleClient)
    client.transport = Transport()
    gateway.providers["local"] = client
    gateway.call("ask", EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")]), [], {})
    assert timeouts == [900, 600]


def test_vision_profile_used_by_ui_setup_status(tmp_path: Path) -> None:
    from litforge.config import Paths
    from litforge.ui.server import App
    cfg = Config(vision={"model": "legacy-ollama"}, models={"vision_profile": "images", "profiles": {
        "images": {"provider": "openai_compatible", "model": "images", "base_url": "http://localhost/v1",
                   "vision": True}}})
    application = App(Paths(tmp_path), cfg)
    assert application.math_setup_status()["model"] == "images"


def test_ollama_profile_timeout(conn: Any) -> None:
    gateway = ModelGateway(conn, Config())
    profile = ModelProfileConfig(provider="ollama", model="m", timeout_s=42)
    assert gateway._default_provider("custom", profile).timeout_s == 42


@pytest.mark.parametrize("response", [b'not a llama router', b'[]', b'{"data": "wrong"}'])
def test_doctor_foreign_nonrouter_response(monkeypatch: pytest.MonkeyPatch, response: bytes) -> None:
    from litforge import doctor
    monkeypatch.setattr(doctor, "open_url", lambda *a, **kw: io.BytesIO(response))
    profile = Config(models={"servers": {"llamacpp": {}}, "local": {
        "provider": "openai_compatible", "server": "llamacpp", "model": "m"}}).models.local
    result = doctor.check_model_profile("local", profile)
    assert "foreign server" in result.detail.lower() and "8093" in result.detail
