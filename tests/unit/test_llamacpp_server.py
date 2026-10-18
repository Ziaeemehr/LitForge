from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from litforge.analysis.gateway import GatewayError, ModelGateway
from litforge.config import Config, LlamaCppServerConfig, ModelProfileConfig
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.llm.llamacpp_server import LlamaCppServer, ProcessIdentity


class Fake:
    def __init__(self) -> None:
        self.now = 0.0
        self.spawned: list[list[str]] = []
        self.signals: list[int] = []
        self.mode = "down"
        self.ready = True
        self.identity = ProcessIdentity(pid=123, started="start", command=["llama-server", "--models-preset"])

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def probe(self, url: str) -> dict[str, Any] | None:
        if self.mode == "down":
            return None
        if self.mode == "foreign":
            return {"unknown": True}
        if url.endswith("/health"):
            return {"status": "ok"}
        return {"data": [{"id": "gemma4", "status": {"value": "unloaded"},
                          "architecture": {"input_modalities": ["text", "image"]}}]}

    def spawn(self, command: list[str], log: Path) -> int:
        self.spawned.append(command)
        self.identity.command = command
        if self.ready:
            self.mode = "up"
        return 123

    def server(self, root: Path, **options: Any) -> LlamaCppServer:
        return LlamaCppServer(root, probe=self.probe, spawner=self.spawn, clock=self.clock,
                             sleep=self.sleep, which=lambda name: "/bin/llama-server",
                             process=lambda pid: self.identity, terminate=self.signals.append,
                             kill=lambda pid: None, **options)


def test_start_and_own_stop(tmp_path: Path) -> None:
    fake = Fake()
    server = fake.server(tmp_path)
    cfg = LlamaCppServerConfig(managed=True)
    assert server.ensure_running(cfg, "gemma4")
    assert len(fake.spawned) == 1
    command = fake.spawned[0]
    assert command[1:] == ["--models-preset", str(tmp_path / "llamacpp-models.ini"), "--models-max", "1",
                           "--host", "127.0.0.1", "--port", "8093", "--sleep-idle-seconds", "900"]
    state = json.loads((tmp_path / "state/llama-server.json").read_text())
    assert state["pid"] == 123 and state["started"] == "start"
    assert server.status(cfg)["models"][0]["id"] == "gemma4"
    assert server.ensure_running(cfg, "gemma4")  # unloaded first request needs a long timeout
    assert len(fake.spawned) == 1
    assert server.stop(cfg) and fake.signals == [123]


@pytest.mark.parametrize("mode", ["foreign", "up"])
def test_foreign_port_never_spawned_or_stopped(tmp_path: Path, mode: str) -> None:
    fake = Fake()
    fake.mode = mode
    server = fake.server(tmp_path)
    cfg = LlamaCppServerConfig(managed=True)
    if mode == "foreign":
        with pytest.raises(GatewayError, match="port 8093 is used by another server"):
            server.ensure_running(cfg, "gemma4")
    else:
        server.ensure_running(cfg, "gemma4")
        with pytest.raises(GatewayError, match="another server"):
            server.ensure_running(cfg, "not-in-preset")
    assert not server.stop(cfg)
    assert fake.spawned == fake.signals == []


@pytest.mark.parametrize("mismatch", ["pid", "started", "command", "preset"])
def test_stop_rejects_stale_or_unrelated_identity(tmp_path: Path, mismatch: str) -> None:
    fake = Fake()
    server = fake.server(tmp_path)
    cfg = LlamaCppServerConfig(managed=True)
    server.ensure_running(cfg, "gemma4")
    if mismatch == "pid":
        fake.identity.pid = 99
    elif mismatch == "started":
        fake.identity.started = "reused-pid"
    elif mismatch == "command":
        fake.identity.command[0] = "other-program"
    else:
        fake.identity.command = ["llama-server", "--models-preset", "/other/preset.ini"]
    assert not server.stop(cfg) and fake.signals == []


def test_not_managed_missing_executable_and_timeout(tmp_path: Path) -> None:
    fake = Fake()
    with pytest.raises(GatewayError, match="start"):
        fake.server(tmp_path).ensure_running(LlamaCppServerConfig(), "gemma4")
    server = LlamaCppServer(tmp_path, probe=fake.probe, which=lambda name: None)
    with pytest.raises(GatewayError, match="brew install llama.cpp"):
        server.ensure_running(LlamaCppServerConfig(managed=True), "gemma4")
    fake.ready = False
    with pytest.raises(GatewayError, match="timed out"):
        fake.server(tmp_path).ensure_running(LlamaCppServerConfig(managed=True, start_timeout_s=1), "gemma4")
    assert fake.now >= 1


def test_lock_rechecks_before_spawning(tmp_path: Path) -> None:
    from contextlib import contextmanager
    fake = Fake()
    @contextmanager
    def lock(*args: Any, **kwargs: Any) -> Any:
        fake.mode = "up"  # another process started it while this one waited
        yield
    server = fake.server(tmp_path, lock=lock)
    server.ensure_running(LlamaCppServerConfig(managed=True), "gemma4")
    assert fake.spawned == []


@pytest.mark.parametrize("reason", ["routing", "cloud", "approval", "sensitive", "budget", "context", "vision"])
def test_gateway_guard_precedes_managed_start(conn: sqlite3.Connection, reason: str) -> None:
    from litforge.llm.budget import Pricing
    cfg = Config(models={"servers": {"llamacpp": {"managed": True}}, "profiles": {
        "managed": {"provider": "openai_compatible", "model": "gemma4", "server": "llamacpp", "vision": True}}})
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")])
    chosen = "managed"
    if reason in {"cloud", "approval", "sensitive", "budget"}:
        chosen = "remote"
        cfg.models.profiles["remote"] = ModelProfileConfig(provider="openai_compatible", model="api", remote=True,
            base_url="https://fake.invalid", api_key_env="FAKE", vision=True,
            pricing=Pricing(input_per_million_usd=1, output_per_million_usd=1))
        cfg.models.policy = "ask_before_cloud"
        cfg.privacy.allow_cloud = reason != "cloud"
        packet.sensitive = reason == "sensitive"
        if reason == "budget":
            cfg.budget.max_per_call_usd = 0
    elif reason == "routing":
        chosen = "unknown"
    elif reason == "context":
        cfg.models.profiles[chosen].context_tokens = 1
    else:
        cfg.models.profiles[chosen].vision = False
    def forbidden(*args: Any) -> Any:
        pytest.fail("managed startup/provider accessed before checks")
    gateway = ModelGateway(conn, cfg, provider_factory=forbidden, ensure_server=forbidden)
    with pytest.raises((ValueError, RuntimeError)):
        gateway.call_vision("ask", packet, [{"role": "user", "content": "test"}], {}, [b"image"], chosen,
                            approved=reason == "budget")


def test_gateway_managed_timeout_and_preview(conn: sqlite3.Connection) -> None:
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    cfg = Config(models={"servers": {"llamacpp": {"managed": True}}, "profiles": {
        "managed": {"provider": "openai_compatible", "model": "gemma4", "server": "llamacpp"}}})
    calls: list[float | str] = []
    class Transport:
        def request(self, url: str, headers: Any, body: Any, timeout_s: float) -> Any:
            calls.append(timeout_s)
            return {"choices": [{"message": {"content": "{}"}}]}
    def ensure(cfg: Any, model: str) -> bool:
        calls.append(model)
        return True
    client = OpenAICompatibleClient("http://fake", "gemma4", transport=Transport())
    gateway = ModelGateway(conn, cfg, {"managed": client}, ensure_server=ensure)
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")])
    preview = gateway.preview("ask", packet, [], "managed")
    assert "starts llama-server" in preview.model_notice and "download" in preview.model_notice
    assert calls == []
    gateway.call("ask", packet, [], {}, "managed")
    assert calls == ["gemma4", 900]


def test_unloaded_then_loaded_timeout_and_unmanaged_profile(conn: sqlite3.Connection) -> None:
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    timeouts: list[float] = []
    class Transport:
        def request(self, url: str, headers: Any, body: Any, timeout_s: float) -> Any:
            timeouts.append(timeout_s)
            return {"choices": [{"message": {"content": "{}"}}]}
    cfg = Config(models={"servers": {"llamacpp": {"managed": True}}, "local": {
        "provider": "openai_compatible", "model": "gemma4", "server": "llamacpp"}})
    cold = iter([True, False])
    gateway = ModelGateway(conn, cfg, {"local": OpenAICompatibleClient("http://fake", "m", transport=Transport())},
                           ensure_server=lambda cfg, model: next(cold))
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", page=1, text="test")])
    gateway.call("ask", packet, [], {})
    gateway.call("ask", packet, [], {})
    assert timeouts == [900, 120]


def test_real_file_lock_excludes_second_owner(tmp_path: Path) -> None:
    from litforge.llm.files import file_lock
    fake = Fake()
    lock = tmp_path / "lock"
    with file_lock(lock), pytest.raises(TimeoutError), file_lock(lock, 0.2, clock=fake.clock, sleep=fake.sleep):
        pytest.fail("lock entered twice")
    with file_lock(lock, 0):
        pass


def test_title_abstract_screening_uses_setup_gateway(conn: sqlite3.Connection) -> None:
    from lens_fakes import seed

    from litforge.screening.criteria import Criteria, save_criteria
    from litforge.screening.llm import FakeLlm
    from litforge.screening.run import screen_run
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="gain"))
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES ('d','proj','search','ok','now')")
    conn.execute("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project) VALUES ('d','p',1,1,1)")
    starts: list[str] = []
    cfg = Config(models={"servers": {"llamacpp": {"managed": True}}, "local": {
        "provider": "openai_compatible", "model": "qwen3-4b", "server": "llamacpp"}})
    fake = FakeLlm(lambda messages: {"relevance": 4, "inclusion": [], "exclusion": [], "reason": "fits"})
    def ensure(cfg: Any, model: str) -> bool:
        starts.append(model)
        return False
    gateway = ModelGateway(conn, cfg, {"local": fake}, ensure_server=ensure)
    legacy = FakeLlm(lambda messages: pytest.fail("legacy client used for managed setup"))
    report = screen_run(conn, legacy, "d", 1, gateway=gateway)
    assert report.screened == 1 and starts == ["qwen3-4b"]


def test_console_explicit_model_overrides_task_map(conn: sqlite3.Connection) -> None:
    from litforge.console.planner import preview_planner_call
    cfg = Config(models={"profiles": {"chosen": {"provider": "ollama", "model": "chosen-model"}},
                         "tasks": {"console_plan": {"profile": "local"}}})
    preview = preview_planner_call(ModelGateway(conn, cfg), "test request", profile="chosen")
    assert preview.profile == "chosen" and preview.model == "chosen-model"


def test_process_inspection_preserves_preset_paths_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import llamacpp_server
    def run(args: list[str]) -> str:
        if args[-1] == "lstart=":
            return "Sun Oct 18 12:00:00 2026"
        if args[-1] == "comm=":
            return "/Applications/Local Tools/llama-server"
        return ("/Applications/Local Tools/llama-server --models-preset /data/My Library/llamacpp-models.ini "
                "--models-max 1 --host 127.0.0.1 --port 8080")
    monkeypatch.setattr(llamacpp_server, "run_command", run)
    identity = llamacpp_server.process_identity(2147483647)
    assert identity is not None
    assert identity.command[0] == "/Applications/Local Tools/llama-server"
    assert identity.command[identity.command.index("--models-preset") + 1] == "/data/My Library/llamacpp-models.ini"


def test_uninspectable_live_process_never_causes_a_second_spawn(tmp_path: Path) -> None:
    fake = Fake()
    fake.ready = False
    server = LlamaCppServer(tmp_path, probe=fake.probe, spawner=fake.spawn, process=lambda pid: None,
                            alive=lambda pid: True, clock=fake.clock, sleep=fake.sleep,
                            which=lambda name: "/bin/llama-server")
    cfg = LlamaCppServerConfig(managed=True, start_timeout_s=0.2)
    with pytest.raises(GatewayError, match="timed out"):
        server.ensure_running(cfg, "gemma4")
    with pytest.raises(GatewayError, match="verify"):
        server.ensure_running(cfg, "gemma4")
    assert len(fake.spawned) == 1
    assert not server.stop(cfg)
