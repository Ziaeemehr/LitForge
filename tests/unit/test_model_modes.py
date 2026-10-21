"""Model modes: Local, Cloud, Hybrid and Manual set the tier, the policy and every task's model in one choice."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litforge.config import TASK_NAMES, load_config
from litforge.llm.hardware import Hardware
from litforge.llm.model_setup import BULK_TASKS, setup_proposal
from litforge.llm.pricing import OpenRouterPricing

FIXTURE = Path(__file__).parents[1] / "fixtures/models/openrouter.json"
APPLE = Hardware(os="Darwin", arch="arm64", ram_gb=36, unified_memory=True, ollama="/fake/ollama",
                 llama_server="/fake/llama-server")


def catalogue(tmp_path: Path) -> list[dict]:
    return OpenRouterPricing(tmp_path, fetch=lambda: json.loads(FIXTURE.read_text()), clock=lambda: 1000).catalogue()


def apply(tmp_path: Path, **kw):
    plan = setup_proposal(tmp_path, APPLE, **kw)
    assert plan.available, plan.notes
    plan.change.apply()
    return load_config(data_root=tmp_path)


def test_local_mode_keeps_every_task_on_this_computer(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("privacy:\n  allow_cloud: true\n")
    cfg = apply(tmp_path, mode="local")
    assert cfg.models.mode == "local" and cfg.models.setup_tier == "large"
    assert cfg.models.policy == "local_only" and not cfg.privacy.allow_cloud
    profiles = {**cfg.models.profiles, "local": cfg.models.local}
    assert all(not profiles[cfg.models.tasks[t].profile].remote for t in TASK_NAMES)


def test_cloud_mode_puts_light_work_on_the_cloud_too(tmp_path: Path) -> None:
    cfg = apply(tmp_path, mode="cloud", cheap="fixture/vision-cheap", strong="fixture/strong",
                catalogue=catalogue(tmp_path))
    assert cfg.models.mode == "cloud" and cfg.models.policy == "hybrid"
    assert cfg.models.tasks["screen"].profile == "cloud_cheap"  # even with Ollama installed
    assert cfg.models.tasks["page_rewrite"].profile == "cloud_cheap"
    assert cfg.models.tasks["write_cite"].profile == "cloud_strong"
    assert not cfg.privacy.allow_cloud  # consent stays a separate, explicit step


def test_cloud_mode_keeps_a_consent_already_given(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("privacy:\n  allow_cloud: true\n")
    cfg = apply(tmp_path, mode="cloud", cheap="fixture/vision-cheap", catalogue=catalogue(tmp_path))
    assert cfg.privacy.allow_cloud and cfg.models.tasks["ask"].profile == "cloud_cheap"


def test_hybrid_mode_bulk_work_local_quality_work_on_the_cloud(tmp_path: Path) -> None:
    cfg = apply(tmp_path, mode="hybrid", strong="fixture/strong", catalogue=catalogue(tmp_path))
    assert cfg.models.mode == "hybrid" and cfg.models.policy == "hybrid" and cfg.models.setup_tier == "large"
    profiles = {**cfg.models.profiles, "local": cfg.models.local}
    for task in TASK_NAMES:
        remote = profiles[cfg.models.tasks[task].profile].remote
        assert remote == (task not in BULK_TASKS), task
    assert cfg.models.tasks["ask"].profile == "cloud_strong"
    assert not profiles[cfg.models.tasks["page_rewrite"].profile].remote  # Better math stays local


def test_hybrid_needs_a_cloud_model(tmp_path: Path) -> None:
    plan = setup_proposal(tmp_path, APPLE, mode="hybrid")
    assert not plan.available and "cloud model" in plan.notes.lower()


def test_manual_mode_only_records_the_choice(tmp_path: Path) -> None:
    apply(tmp_path, mode="local")
    before = load_config(data_root=tmp_path).models.tasks
    cfg = apply(tmp_path, mode="manual")
    assert cfg.models.mode == "manual" and cfg.models.tasks == before


def test_switching_modes_keeps_a_task_the_user_assigned(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("models:\n  tasks:\n    ask:\n      profile: local\n")
    cfg = apply(tmp_path, mode="cloud", cheap="fixture/vision-cheap", catalogue=catalogue(tmp_path))
    assert cfg.models.tasks["ask"].profile == "local"  # the user's own choice is never overwritten
    assert cfg.models.tasks["screen"].profile == "cloud_cheap"


def test_assigning_a_task_by_hand_switches_to_manual(tmp_path: Path) -> None:
    from litforge.config import Paths
    from litforge.ui.api import ModelsPanel
    apply(tmp_path, mode="local")
    paths = Paths(tmp_path)
    panel = ModelsPanel(paths, load_config(data_root=tmp_path), hardware=lambda root: APPLE)
    preview = panel.preview({"action": "assign", "name": "local", "tasks": ["ask"]})
    panel.apply(preview["token"], True)
    assert load_config(data_root=tmp_path).models.mode == "manual"


@pytest.mark.parametrize("mode", ["local", "cloud", "hybrid", "manual"])
def test_the_panel_accepts_modes(tmp_path: Path, mode: str) -> None:
    from litforge.config import Paths
    from litforge.ui.api import ModelsPanel
    panel = ModelsPanel(Paths(tmp_path), load_config(data_root=tmp_path), hardware=lambda root: APPLE)
    panel.catalogue = OpenRouterPricing(tmp_path, fetch=lambda: json.loads(FIXTURE.read_text()), clock=lambda: 1000)
    body = {"action": "setup", "mode": mode, "cheap": "fixture/vision-cheap", "strong": "fixture/strong"}
    assert panel.preview(body)["token"]


def test_math_high_says_when_it_runs_on_the_cloud_and_what_a_page_costs(tmp_path: Path) -> None:
    from litforge.llm.model_setup import math_high_cloud
    assert math_high_cloud(apply(tmp_path, mode="local")) is None
    cfg = apply(tmp_path, mode="cloud", cheap="fixture/vision-cheap", catalogue=catalogue(tmp_path))
    info = math_high_cloud(cfg)
    assert info is not None and info["model"] == "fixture/vision-cheap"
    price = cfg.models.profiles["cloud_cheap"].pricing
    assert price is not None
    assert info["usd_per_page"] == pytest.approx((1024 * price.input_per_million_usd
                                                  + 4000 * price.output_per_million_usd) / 1_000_000)
    assert math_high_cloud(apply(tmp_path, mode="hybrid", strong="fixture/strong",
                                 catalogue=catalogue(tmp_path))) is None  # Hybrid keeps Better math local


def test_the_panel_reports_the_mode_and_the_cloud_cost_of_math_high(tmp_path: Path) -> None:
    from litforge.config import Paths
    from litforge.ui.api import ModelsPanel
    cfg = apply(tmp_path, mode="cloud", cheap="fixture/vision-cheap", catalogue=catalogue(tmp_path))
    panel = ModelsPanel(Paths(tmp_path), cfg, hardware=lambda root: APPLE)
    panel.server.status = lambda cfg: {"running": False, "models": []}  # type: ignore[method-assign]
    info = panel.info()
    assert info["mode"] == "cloud" and info["math_high_cloud"]["model"] == "fixture/vision-cheap"
