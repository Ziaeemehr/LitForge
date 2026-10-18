from __future__ import annotations

import json
from pathlib import Path

import pytest

from litforge.config import load_config
from litforge.llm.hardware import Hardware
from litforge.llm.model_setup import cloud_estimate, setup_proposal
from litforge.llm.pricing import OpenRouterPricing

FIXTURE = Path(__file__).parents[1] / 'fixtures/models/openrouter.json'


def catalogue(tmp_path: Path) -> OpenRouterPricing:
    return OpenRouterPricing(tmp_path, fetch=lambda: json.loads(FIXTURE.read_text()), clock=lambda: 1000)


def test_catalogue_filters_sorts_and_caches(tmp_path: Path) -> None:
    prices = catalogue(tmp_path)
    items = prices.catalogue()
    assert [m['id'] for m in items] == ['fixture/vision-cheap', 'fixture/strong', 'free']  # $0 sorts last
    assert prices.catalogue(vision=True)[0]['input_per_million_usd'] == pytest.approx(.1)
    assert prices.catalogue(max_input_price=.5, q='fixture/') == prices.catalogue(vision=True)
    assert prices.catalogue(q='analysis')[0]['context_length'] == 65536
    assert prices.catalogue(vision=True)[0]['name'].startswith('<img')
    prices.fetch = lambda: pytest.fail('daily cache refetched')
    assert prices.lookup('fixture/vision-cheap').pricing is not None
    assert prices.catalogue()


@pytest.mark.parametrize('ollama', [None, '/fake/ollama'])
def test_cloud_plan_ownership_prices_and_explicit_permission(tmp_path: Path, ollama: str | None) -> None:
    hw = Hardware(os='Linux', arch='x86_64', ram_gb=8, ollama=ollama)
    plan = setup_proposal(tmp_path, hw, 'cloud', cheap='fixture/vision-cheap', strong='fixture/strong',
                          catalogue=catalogue(tmp_path).catalogue())
    assert plan.available and plan.recommended_tier == 'cloud'
    assert 'matrix' in plan.notes.lower() and 'rewrite' in plan.notes.lower()
    plan.change.apply()
    cfg = load_config(data_root=tmp_path)
    assert not cfg.privacy.allow_cloud
    assert cfg.models.policy == 'ask_before_cloud'
    assert cfg.models.tasks['screen'].profile == ('local' if ollama else 'cloud_cheap')
    assert cfg.models.tasks['matrix_extract'].profile == 'cloud_cheap'
    assert cfg.models.tasks['ask'].profile == 'cloud_strong'
    assert cfg.models.profiles['cloud_cheap'].vision
    assert cfg.models.profiles['cloud_cheap'].extra_body['provider']['data_collection'] == 'deny'
    again = setup_proposal(tmp_path, hw, 'cloud', cheap='fixture/vision-cheap',
                           catalogue=catalogue(tmp_path).catalogue())
    again.change.apply()
    assert load_config(data_root=tmp_path).models.tasks['ask'].profile == 'cloud_cheap'


def test_small_recommends_cloud_without_fetching_or_choosing_id(tmp_path: Path) -> None:
    plan = setup_proposal(tmp_path, Hardware(os='Linux', arch='x86_64'))
    assert plan.recommended_tier == 'cloud'
    assert not plan.available
    assert not (tmp_path / 'config.yaml').exists()


def test_cloud_requires_catalogued_vision_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='image'):
        setup_proposal(tmp_path, Hardware(os='Linux', arch='x86_64'), 'cloud', cheap='fixture/strong',
                       catalogue=catalogue(tmp_path).catalogue())


def test_cost_arithmetic() -> None:
    item = {'input_per_million_usd': 1., 'output_per_million_usd': 4.}
    cost = cloud_estimate(item, papers=10, columns=5, average_input_tokens=1000, average_output_tokens=100)
    assert cost['matrix_usd'] == pytest.approx(.07)
    assert cost['page_rewrite_usd'] == pytest.approx(.017024)


def test_recorded_documented_openrouter_catalogue(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / 'fixtures/openrouter_models.json'
    prices = OpenRouterPricing(tmp_path, fetch=lambda: json.loads(fixture.read_text()), clock=lambda: 1000)
    item = prices.catalogue()[0]
    assert item['id'] == 'openai/gpt-4'
    assert item['input_per_million_usd'] == 30


def test_cloud_setup_cli_from_live_catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from litforge.cli import app
    from litforge.llm import commands
    monkeypatch.setattr(commands, 'OpenRouterPricing', lambda root: catalogue(root))
    monkeypatch.setattr(commands, 'detect_hardware', lambda root: Hardware(os='Linux', arch='x86_64', ram_gb=8))
    result = CliRunner().invoke(app, ['--data-dir', str(tmp_path), 'models', 'setup', '--tier', 'cloud',
        '--cheap', 'fixture/vision-cheap', '--strong', 'fixture/strong', '--yes'])
    assert result.exit_code == 0, result.output
    assert 'Typical matrix' in result.output and 'page rewrite' in result.output
    assert not load_config(data_root=tmp_path).privacy.allow_cloud


def test_recorded_live_vision_catalogue_and_prices(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / 'fixtures/models/openrouter-recorded.json'
    prices = OpenRouterPricing(tmp_path, fetch=lambda: json.loads(fixture.read_text()), clock=lambda: 1000)
    entries = prices.catalogue(vision=True)
    assert len(entries) == 2 and entries[0]['input_per_million_usd'] == pytest.approx(.15)
    assert entries[1]['output_per_million_usd'] == pytest.approx(3)


def test_removing_cloud_vision_profile_clears_image_route(tmp_path: Path) -> None:
    from litforge.llm.model_setup import remove_model_change
    plan = setup_proposal(tmp_path, Hardware(os='Linux', arch='x86_64'), 'cloud', cheap='fixture/vision-cheap',
                          catalogue=catalogue(tmp_path).catalogue())
    plan.change.apply()
    cfg = load_config(data_root=tmp_path)
    remove_model_change(tmp_path, 'cloud_cheap', cfg).apply()
    assert load_config(data_root=tmp_path).models.vision_profile is None


def test_catalogue_keeps_text_only_models_and_sorts_zero_price_last(tmp_path: Path) -> None:
    def model(mid: str, out: list[str], price: str) -> dict[str, object]:
        return {"id": mid, "name": mid, "context_length": 1000, "pricing": {"prompt": price, "completion": price},
                "architecture": {"input_modalities": ["text", "image"], "output_modalities": out}}

    data = {"data": [model("google/lyria-like", ["text", "audio"], "0"), model("router/free", ["text"], "0"),
                     model("paid/cheap", ["text"], "0.0000001")]}
    prices = OpenRouterPricing(tmp_path, fetch=lambda: data, clock=lambda: 1000)
    assert [m["id"] for m in prices.catalogue(vision=True)] == ["paid/cheap", "router/free"]
    assert prices.catalogue()[1]["label"]
