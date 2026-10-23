from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from litforge.config import Config, Paths, load_config
from litforge.llm.hardware import Hardware
from litforge.ui import api
from litforge.ui.server import App, make_handler


def test_models_panel_preview_apply_and_refresh(tmp_path: Path) -> None:
    hw = Hardware(os="Darwin", arch="arm64", unified_memory=True, ram_gb=24, llama_server="/fake/llama-server")
    panel = api.ModelsPanel(Paths(tmp_path), Config(), hardware=lambda root: hw)
    info = panel.info()
    assert info["proposal"]["tier"] == "large"
    assert not (tmp_path / "config.yaml").exists()
    result = panel.preview({"action": "setup"})
    assert "llamacpp-models.ini" in result["diff"]
    with pytest.raises(ValueError, match="confirm"):
        panel.apply(result["token"], False)
    panel.apply(result["token"], True)
    assert panel.config.models.setup_tier == "large"
    assert load_config(data_root=tmp_path).models.profiles["gemma4"].vision
    with pytest.raises(ValueError):
        panel.apply(result["token"], True)


def test_panel_api_add_cloud_and_remove(tmp_path: Path) -> None:
    panel = api.ModelsPanel(Paths(tmp_path), Config(), hardware=lambda root: Hardware(os="Linux", arch="x86_64"))
    proposal = panel.preview({"action": "add", "provider": "openai", "model": "user-supplied", "name": "api",
                              "tasks": ["ask"], "price_in": 1, "price_out": 2})
    panel.apply(proposal["token"], True)
    assert not panel.config.privacy.allow_cloud
    cloud = panel.preview({"action": "cloud", "enabled": True})
    assert "evidence" in cloud["notice"]
    panel.apply(cloud["token"], True)
    assert panel.config.models.policy == "ask_before_cloud"
    remove = panel.preview({"action": "remove", "name": "api"})
    panel.apply(remove["token"], True)
    assert not panel.config.models.profiles and not panel.config.models.tasks


def handler_request(app: App, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    handler = object.__new__(make_handler(app))
    handler.path = path
    handler.headers = {}  # type: ignore[assignment]
    handler.wfile = io.BytesIO()
    output: list[Any] = []
    handler._json = lambda value, status=200: output.extend([status, value])  # type: ignore[method-assign]
    handler._error = lambda status, value: output.extend([status, value])  # type: ignore[method-assign]
    handler._same_origin = lambda: True  # type: ignore[method-assign]
    handler._body = lambda: body or {}  # type: ignore[method-assign]
    if body is None:
        handler.do_GET()
    else:
        handler.do_POST()
    return output[0], output[1]


def test_models_http_routes_without_sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    application = App(Paths(tmp_path), Config())
    monkeypatch.setattr(application.models, "hardware", lambda root: Hardware(os="Linux", arch="x86_64"))
    code, info = handler_request(application, "/api/models")
    assert code == 200 and "profiles" in info and "server" in info
    code, preview = handler_request(application, "/api/models/preview", {
        "action": "add", "provider": "google", "model": "supplied", "name": "api"})
    assert code == 200 and "GEMINI_API_KEY" in preview["diff"]
    code, _ = handler_request(application, "/api/models/apply", {"token": preview["token"], "confirmed": False})
    assert code == 400
    code, _ = handler_request(application, "/api/models/apply", {"token": preview["token"], "confirmed": True})
    assert code == 200 and application.config.models.profiles["api"].remote
    assert not application.config.privacy.allow_cloud
    assert "api" in json.dumps(handler_request(application, "/api/model-profiles")[1])


def test_quit_and_idle_cleanup_stop_only_the_managed_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    application = App(Paths(tmp_path), Config(models={"servers": {"llamacpp": {"managed": True}}}))
    calls: list[str] = []
    monkeypatch.setattr(application.models.server, "stop", lambda cfg: calls.append("stop") or True)
    application.stop_models()
    assert calls == ["stop"]


def test_remote_panel_diagnostic_needs_separate_approval(tmp_path: Path) -> None:
    cfg = Config(models={"policy": "hybrid", "profiles": {"cloud": {
        "provider": "openai_compatible", "model": "m", "remote": True,
        "base_url": "https://fake.invalid", "api_key_env": "FAKE", "pricing": {
            "input_per_million_usd": 1, "output_per_million_usd": 1}}}}, privacy={"allow_cloud": True})
    application = App(Paths(tmp_path), cfg)
    code, _ = handler_request(application, "/api/models/test", {"profile": "cloud", "approved": False})
    assert code == 400 and application.jobs == {}


def test_models_settings_reject_invalid_fields_without_echoing_secrets(tmp_path: Path) -> None:
    panel = api.ModelsPanel(Paths(tmp_path), Config())
    with pytest.raises(ValueError, match="Invalid model settings") as error:
        panel.preview({"action": "add", "provider": ["SECRET_VALUE"], "model": 42})
    assert "SECRET_VALUE" not in str(error.value)
    assert not (tmp_path / "config.yaml").exists()


@pytest.mark.parametrize("interrupt", [False, True])
def test_serve_cleanup_runs_after_shutdown_without_sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         interrupt: bool) -> None:
    from litforge.ui import server
    cfg = Config(models={"servers": {"llamacpp": {"managed": True}}})
    application = App(Paths(tmp_path), cfg)
    calls: list[str] = []
    class Httpd:
        server_address = ("127.0.0.1", 8765)
        def __init__(self, *args: Any) -> None:
            pass
        def serve_forever(self) -> None:
            calls.append("serve")
        def shutdown(self) -> None:
            pass
        def server_close(self) -> None:
            calls.append("close")
    monkeypatch.setattr(server, "App", lambda *args: application)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Httpd)
    monkeypatch.setattr(server, "_is_litforge", lambda url: False)
    monkeypatch.setattr(server, "stop_when_idle", lambda *args: None)
    if interrupt:  # Ctrl+C reaches the main thread while serve() waits for the server thread
        def ctrl_c(self: Any, timeout: float | None = None) -> bool:
            raise KeyboardInterrupt
        monkeypatch.setattr(server.ServerHandle, "wait", ctrl_c)
    monkeypatch.setattr(application.models.server, "stop", lambda cfg: calls.append("stop-models"))
    server.serve(application.paths, cfg, open_browser=False)
    assert calls == ["serve", "stop-models", "close"]


@pytest.mark.parametrize("tier", ["large", "medium", "small"])
def test_math_preflight_uses_setup_without_legacy_installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          tier: str) -> None:
    cfg = Config(models={"setup_tier": tier}, vision={"model": None if tier == "small" else "qwen2.5vl:7b"})
    application = App(Paths(tmp_path), cfg)
    def forbidden() -> Any:
        pytest.fail("legacy Ollama installer/probe used after model setup")
    monkeypatch.setattr(application.vision_setup, "status", forbidden)
    code, status = handler_request(application, "/api/math-setup")
    assert code == 200 and status["ready"] == (tier != "small")
    assert status["disabled"] == (tier == "small")
    code, _ = handler_request(application, "/api/math-setup", {})
    assert code == 400 and application.jobs == {}
