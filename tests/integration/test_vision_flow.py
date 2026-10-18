import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from litforge.config import Config, Paths
from litforge.export.markdown import export_markdown
from litforge.ingest import ingest_paths
from litforge.parsing.formulas import FORMULAS
from litforge.parsing.run import artifacts_for_project, parse_artifact
from litforge.parsing.vision import VISION, FakeVision, enhance_project
from litforge.projects import create_project


@pytest.fixture()
def project(conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path) -> str:
    pid = create_project(conn, paths, "vis")
    ingest_paths(conn, paths, [corpus / "single_column.pdf"], pid)
    for art in artifacts_for_project(conn, pid):
        parse_artifact(conn, paths, config, art)
    return pid


def _transcribe(png: bytes, page_text: str) -> str:
    return "```markdown\n" + page_text + "\n\n$$\n\\frac{dy}{dt} = A a S(v) - 2 a y - a^2 x \\tag{1}\n$$\n```"


def test_enhance_rewrites_suspicious_pages_and_export_uses_them(
    conn: sqlite3.Connection, paths: Paths, config: Config, project: str
) -> None:
    root, _ = export_markdown(conn, paths, "vis", project)
    assert "math: raw\n" in next((root / "knowledge_base").glob("*.md")).read_text()
    vision = FakeVision(_transcribe)
    report = enhance_project(conn, paths, config, project, vision)
    assert report.papers == 1 and report.pages_improved >= 1 and vision.calls == report.pages_improved
    item = report.items[0]
    assert item["title"] and item["improved"] == report.pages_improved and item["rejected"] == 0

    again = enhance_project(conn, paths, config, project, vision)  # cached: no model call
    assert vision.calls == report.pages_improved and again.pages_improved == report.pages_improved

    root, _ = export_markdown(conn, paths, "vis", project)
    note = next((root / "knowledge_base").glob("*.md")).read_text()
    assert "math: high\n" in note
    assert "\\frac{dy}{dt}" in note and "```markdown" not in note

    # A transcription that does not match the page is rejected and the parser's text is kept.
    out_dir = next(paths.parsed.rglob("vision.json")).parent
    (out_dir / "vision.json").unlink()
    liar = FakeVision(lambda png, text: "Nothing to see here, just a story about cats.")
    bad = enhance_project(conn, paths, config, project, liar)
    assert bad.pages_improved == 0 and bad.items[0]["rejected"] >= 1
    saved = json.loads((out_dir / "vision.json").read_text())
    assert all(p["markdown"] is None for p in saved["pages"].values())
    root, _ = export_markdown(conn, paths, "vis", project)
    note = next((root / "knowledge_base").glob("*.md")).read_text()
    assert "cats" not in note and "math: raw\n" in note


@pytest.mark.parametrize("markdown, expected", [(None, "raw"), ("", "raw"), ("$$x=1$$", "medium")])
def test_export_math_from_formulas(
    conn: sqlite3.Connection, paths: Paths, project: str, markdown: str | None, expected: str
) -> None:
    row = conn.execute("SELECT output_dir FROM parse_runs WHERE is_current = 1").fetchone()
    out_dir = paths.root / row["output_dir"]
    (out_dir / FORMULAS).write_text(json.dumps({"pages": {"1": {"markdown": markdown}}}))
    (out_dir / VISION).write_text(json.dumps({"pages": {"1": {"markdown": None}, "2": {"markdown": ""}}}))
    root, _ = export_markdown(conn, paths, "vis", project)
    assert f"math: {expected}\n" in next((root / "knowledge_base").glob("*.md")).read_text()


def test_page_cache_keeps_provider_model_and_prompt_variants(
    conn: sqlite3.Connection, paths: Paths, config: Config, project: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litforge.parsing import vision as module

    class NamedVision(FakeVision):
        provider = "ollama"

    first = NamedVision(_transcribe, name="same-model")
    initial = enhance_project(conn, paths, config, project, first)
    assert first.calls == initial.pages_improved > 0
    other_provider = NamedVision(_transcribe, name="same-model")
    other_provider.provider = "openai_compatible"
    enhance_project(conn, paths, config, project, other_provider)
    assert other_provider.calls == first.calls
    enhance_project(conn, paths, config, project, first)
    assert first.calls == initial.pages_improved
    other_model = NamedVision(_transcribe, name="new-model")
    enhance_project(conn, paths, config, project, other_model)
    assert other_model.calls == first.calls
    monkeypatch.setattr(module, "PROMPT_VERSION", "next")
    enhance_project(conn, paths, config, project, first)
    assert first.calls == 2 * initial.pages_improved


def test_page_plan_matches_pending_images_and_uses_no_model_transport(
    conn: sqlite3.Connection, paths: Paths, config: Config, project: str,
) -> None:
    from litforge.analysis.gateway import ModelGateway
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    from litforge.parsing.vision import GatewayVision, plan_project

    class Transport:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
            self.calls.append(body)
            return {"choices": [{"message": {"content": "# A page transcription"}}]}

    transport = Transport()
    client = OpenAICompatibleClient("http://local/v1", "vision", transport=transport)
    engine = GatewayVision(ModelGateway(conn, config, {"local_vision": client}))
    previews = plan_project(conn, paths, project, engine)
    assert len(previews) > 0 and transport.calls == []
    assert all(p.image_description == "1 page images" for p in previews)
    enhance_project(conn, paths, config, project, engine)
    assert len(transport.calls) == len(previews)
    assert plan_project(conn, paths, project, engine) == []
