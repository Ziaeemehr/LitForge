"""Regressions from the second managed-models review."""
from __future__ import annotations

import json
import signal
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from litforge.cli import app
from litforge.config import LlamaCppServerConfig, load_config
from litforge.llm.hardware import Hardware
from litforge.llm.llamacpp_server import LlamaCppServer, ProcessIdentity
from litforge.llm.model_setup import api_model_change, api_model_patch, cloud_change, setup_proposal

HW = Hardware(os="Darwin", arch="arm64", ram_gb=36, unified_memory=True,
              llama_server="llama-server", ollama="ollama")
NOTE = ("# Managed in part by LitForge (models setup / models add). Comments elsewhere in this file "
        "are not preserved; the previous version is in config.yaml.bak.")


@pytest.mark.parametrize("tiers", [("large", "medium", "small"), ("medium", "large", "small")])
def test_tier_switch_removes_owned_preserves_user_and_stops_server(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiers: tuple[str, ...]) -> None:
    stops = []
    monkeypatch.setattr(LlamaCppServer, "stop", lambda self, cfg: stops.append(cfg) or True)
    setup_proposal(tmp_path, HW, tiers[0]).change.apply()
    api_model_change(tmp_path, api_model_patch("openai", "user-model", name="mine", tasks=["ask"])).apply()
    for tier in tiers[1:]:
        setup_proposal(tmp_path, HW, tier).change.apply()
        cfg = load_config(data_root=tmp_path)
        assert cfg.models.tasks["ask"].profile == "mine"
        assert "mine" in cfg.models.profiles
        expected = {"gemma4", "qwen3-4b"} if tier == "large" else {"ollama_vision"} if tier == "medium" else set()
        assert set(cfg.models.profiles) == expected | {"mine"}
        assert bool(cfg.models.servers) == (tier == "large")
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert set(raw["models"]["setup_owned"]["profiles"]) == expected
        assert "ask" not in raw["models"]["setup_owned"]["tasks"]
    assert len(stops) == 1


def test_backup_exact_previous_bytes_header_and_diff(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = b"# precious comment\r\nprivacy: {allow_cloud: false} # inline\r\n"
    path.write_bytes(original)
    change = cloud_change(tmp_path, True)
    assert NOTE in change.diff
    change.apply()
    assert path.with_suffix(".yaml.bak").read_bytes() == original
    assert path.read_text().startswith(NOTE + "\n")
    previous = path.read_bytes()
    cloud_change(tmp_path, False).apply()
    assert path.with_suffix(".yaml.bak").read_bytes() == previous


@pytest.mark.parametrize("reuse_at", [2, 3, None])
def test_stop_rechecks_identity_before_each_signal(tmp_path: Path, reuse_at: int | None) -> None:
    cfg = LlamaCppServerConfig(managed=True)
    preset = str(tmp_path / cfg.preset)
    identity = ProcessIdentity(pid=123, started="original", command=["llama-server", "--models-preset", preset])
    state = tmp_path / "state" / "llama-server.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"pid": 123, "started": "original", "preset": preset}))
    checks = 0
    now = [0.0]
    signals = []
    def process(pid: int) -> ProcessIdentity:
        nonlocal checks
        checks += 1
        return identity.model_copy(update={"started": "reused"}) if reuse_at and checks >= reuse_at else identity
    def sleep(seconds: float) -> None:
        now[0] += seconds
    server = LlamaCppServer(tmp_path, process=process, terminate=lambda pid: signals.append(signal.SIGTERM),
                           kill=lambda pid: signals.append(signal.SIGKILL), clock=lambda: now[0], sleep=sleep)
    server.stop(cfg)
    assert signals == ([] if reuse_at == 2 else [signal.SIGTERM] if reuse_at == 3 else
                       [signal.SIGTERM, signal.SIGKILL])
    if signals:
        assert now[0] >= 5


def test_pricing_fetch_and_write_are_under_cross_process_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import pricing
    locked = []
    fetches = []
    @contextmanager
    def lock(path: Path, *args: Any, **kwargs: Any) -> Any:
        locked.append(path)
        yield
        locked.pop()
    def fetch() -> Any:
        fetches.append(bool(locked))
        return {"data": []}
    write = pricing.atomic_write
    def locked_write(path: Path, content: str) -> None:
        assert locked
        write(path, content)
    monkeypatch.setattr(pricing, "atomic_write", locked_write)
    monkeypatch.setattr(pricing, "file_lock", lock)
    pricing.OpenRouterPricing(tmp_path, fetch=fetch).lookup("missing")
    assert fetches == [True]
    assert (tmp_path / "openrouter-pricing.json").exists()


def test_status_one_line_preset_only_and_raw_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_proposal(tmp_path, HW, "large").change.apply()
    report = {"running": True, "owned": True, "models": [
        {"id": "gemma4", "status": {"value": "loaded", "args": ["verbose"]}},
        {"id": "qwen3-4b", "status": {"value": "unloaded"}},
        {"id": "hf-cache-extra", "status": "loaded"}], "base_url": "http://localhost:8093/v1",
        "preset_models": ["gemma4", "qwen3-4b"]}
    monkeypatch.setattr(LlamaCppServer, "status", lambda *args: report)
    args = ["--data-dir", str(tmp_path), "models", "status"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ("llama-server running (managed by LitForge) · gemma4: loaded · qwen3-4b: unloaded")
    raw = CliRunner().invoke(app, [*args, "--json"])
    assert "hf-cache-extra" in raw.output and "verbose" in raw.output


def test_list_hides_unused_builtin_vision(tmp_path: Path) -> None:
    setup_proposal(tmp_path, HW, "large").change.apply()
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "models", "list"])
    assert result.exit_code == 0, result.output
    assert "local_vision:" not in result.output


def test_pricing_rechecks_disk_after_another_process_refreshes(tmp_path: Path) -> None:
    from litforge.llm.pricing import OpenRouterPricing
    calls = []
    now = [100000.0]
    def fetch() -> Any:
        calls.append(1)
        return {"data": []}
    pricing = OpenRouterPricing(tmp_path, fetch=fetch, clock=lambda: now[0])
    pricing.lookup("missing")
    now[0] += 86401
    pricing.path.write_text(json.dumps({"fetched_at": now[0], "catalogue": {"data": []}}))
    pricing.lookup("missing")
    assert calls == [1]


def test_config_note_stays_visible_in_later_diffs(tmp_path: Path) -> None:
    setup_proposal(tmp_path, HW, "large").change.apply()
    change = api_model_change(tmp_path, api_model_patch("google", "example", name="cloud"))
    assert NOTE in change.diff


def test_setup_preserves_preexisting_user_entries_even_on_name_collision(tmp_path: Path) -> None:
    original = {"models": {"profiles": {"gemma4": {"provider": "ollama", "model": "custom"}},
                           "tasks": {"ask": {"profile": "gemma4"}},
                           "servers": {"llamacpp": {"managed": False, "port": 8888}}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(original))
    setup_proposal(tmp_path, HW, "large").change.apply()
    setup_proposal(tmp_path, HW, "small").change.apply()
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.profiles["gemma4"].model == "custom"
    assert cfg.models.tasks["ask"].profile == "gemma4"
    assert cfg.models.servers["llamacpp"].port == 8888


def test_status_reads_only_preset_aliases_for_summary(tmp_path: Path) -> None:
    from litforge.llm.llamacpp_server import status_line
    (tmp_path / "llamacpp-models.ini").write_text("[gemma4]\nhf-repo = example\n[qwen3-4b]\nhf-repo = other\n")
    def probe(url: str) -> Any:
        return {"status": "ok"} if url.endswith("/health") else {"data": [
            {"id": "gemma4", "status": {"value": "loaded", "args": ["verbose"]}},
            {"id": "qwen3-4b", "status": "unloaded"}, {"id": "hf-extra", "status": "loaded"}]}
    report = LlamaCppServer(tmp_path, probe=probe).status(LlamaCppServerConfig())
    assert status_line(report) == "llama-server running (external) · gemma4: loaded · qwen3-4b: unloaded"
    assert len(report["models"]) == 3


def test_pricing_two_processes_fetch_catalogue_once(tmp_path: Path) -> None:
    import subprocess
    import sys
    script = '''
import sys, time
from pathlib import Path
from litforge.llm.pricing import OpenRouterPricing
root = Path(sys.argv[1])
def fetch():
    with (root / 'fetches').open('a') as stream:
        stream.write('fetch\\n')
    time.sleep(0.1)
    return {'data': [{'id': 'example', 'pricing': {'prompt': '0.000001', 'completion': '0.000002'}}]}
info = OpenRouterPricing(root, fetch=fetch).lookup('example')
assert info.pricing.input_per_million_usd == 1
'''
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    try:
        for child in children:
            _, errors = child.communicate(timeout=15)
            assert child.returncode == 0, errors.decode()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
    assert (tmp_path / "fetches").read_text().splitlines() == ["fetch"]
