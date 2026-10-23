from __future__ import annotations

import sqlite3

import pytest

from litforge.analysis.gateway import STRONGEST_LOCAL, ModelGateway
from litforge.config import Config, ModelProfileConfig, TaskModelConfig
from litforge.llm.evidence import EvidenceItem, EvidencePacket

INSTALLED = [("qwen3:4b", 2_500_000_000), ("qwen3:8b", 5_200_000_000)]
CLOUD = ModelProfileConfig(provider="openai_compatible", model="big/model", remote=True,
                           base_url="https://example.invalid/v1", api_key_env="KEY")


def gateway(conn: sqlite3.Connection, config: Config | None = None,
            installed: list[tuple[str, int]] = INSTALLED) -> ModelGateway:
    return ModelGateway(conn, config or Config(), local_models=lambda: installed)


def test_questions_go_to_the_largest_installed_model_and_bulk_work_stays_local(conn: sqlite3.Connection) -> None:
    gw = gateway(conn)
    for task in ("ask", "write_chat"):
        name, cfg = gw.resolve(task)
        assert (name, cfg.model, cfg.provider, cfg.remote) == (STRONGEST_LOCAL, "qwen3:8b", "ollama", False)
    for task in ("screen", "matrix_extract", "query_translate", "write_rewrite"):
        assert gw.resolve(task)[1].model == "qwen3:4b"
    packet = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", text="text", page=1)])
    assert gw.preview("ask", packet, [{"role": "user", "content": "q"}]).model == "qwen3:8b"


@pytest.mark.parametrize("installed", [[], [("qwen3:4b", 2_500_000_000)], [("tinyllama", 600_000_000)]])
def test_nothing_larger_keeps_the_configured_local_model(conn: sqlite3.Connection,
                                                         installed: list[tuple[str, int]]) -> None:
    assert gateway(conn, installed=installed).resolve("ask")[0] == "local"


def test_the_users_choice_and_the_cloud_come_first(conn: sqlite3.Connection) -> None:
    mapped = Config()
    mapped.models.tasks["ask"] = TaskModelConfig(profile="local")
    assert gateway(conn, mapped).resolve("ask")[0] == "local"
    assert gateway(conn).resolve("ask", profile="local")[0] == "local"
    cloud = Config()
    cloud.models.profiles["cloud_strong"] = CLOUD
    assert gateway(conn, cloud).resolve("ask")[0] == STRONGEST_LOCAL  # cloud not enabled: never sent there
    cloud.privacy.allow_cloud = True
    assert gateway(conn, cloud).resolve("write_chat")[0] == "cloud_strong"
    assert gateway(conn, cloud).resolve("screen")[0] == "local"


def test_injected_providers_do_not_look_for_installed_models(conn: sqlite3.Connection) -> None:
    gw = ModelGateway(conn, Config(), providers={"local": object()})  # type: ignore[dict-item]
    assert gw.resolve("ask")[0] == "local"



REAL_INSTALLED = ModelGateway._installed_models  # kept before the autouse fixture replaces it


def test_installed_models_skip_embeddings_images_and_what_does_not_fit(conn: sqlite3.Connection,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import hardware
    from litforge.llm.hardware import Hardware
    from litforge.screening.llm import OllamaClient
    tags = {"models": [
        {"name": "qwen3:8b", "size": 5_200_000_000, "details": {"families": ["qwen3"]}},
        {"name": "qwen2.5vl:7b", "size": 6_000_000_000, "details": {"families": ["qwen25vl"]}},
        {"name": "llava:13b", "size": 8_000_000_000, "details": {"families": ["llama", "clip"]}},
        {"name": "nomic-embed-text", "size": 270_000_000, "details": {"families": ["nomic-bert"]}},
        {"name": "llama3:70b", "size": 40_000_000_000, "details": {"families": ["llama"]}}]}
    monkeypatch.setattr(OllamaClient, "_call", lambda self, path, body=None: tags)
    monkeypatch.setattr(hardware, "detect_hardware",
                        lambda root: Hardware(os="linux", arch="x86_64", ram_gb=32, gpu_vram_gb=12))
    from litforge.analysis import gateway as gateway_module
    monkeypatch.setattr(gateway_module, "_INSTALLED", {})
    gw = ModelGateway(conn, Config(), local_models=lambda: [])
    assert [name for name, _ in REAL_INSTALLED(gw)] == ["qwen3:8b"]
    # Each web request makes a gateway: the answer is reused instead of asking Ollama again.
    monkeypatch.setattr(OllamaClient, "_call", lambda self, path, body=None: (_ for _ in ()).throw(OSError("down")))
    assert [name for name, _ in REAL_INSTALLED(ModelGateway(conn, Config()))] == ["qwen3:8b"]
    monkeypatch.setattr(gateway_module, "_INSTALLED", {})
    assert REAL_INSTALLED(gw) == []
