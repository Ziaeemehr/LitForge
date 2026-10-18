"""Regressions from the Qwen/Gemma comparison on real papers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from conftest import LOREM, _paragraph
from lens_fakes import seed

from litforge.analysis.ask import SentenceDraft, validate_sentences
from litforge.analysis.audit import AuditDraft
from litforge.analysis.claims import ClaimDraft
from litforge.analysis.figure_commands import _print_analysis
from litforge.analysis.figures import MODELS, _prompt, analyze_asset, asset_analyses, plan_asset
from litforge.analysis.gaps import GapSentence
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.matrix import CellDraft, MatrixColumn, validate_cell
from litforge.config import Config, Paths
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.llm.openai_compatible import OpenAICompatibleClient
from litforge.parsing.pymupdf_parser import Block, Line, PyMuPDFParser, _group_equations
from litforge.parsing.vision import POST_VERSION, PROMPT_VERSION, FakeVision, GatewayVision, enhance_document, plausible


def test_single_column_equation_crop_covers_left_center_and_label(tmp_path: Path) -> None:
    pdf = tmp_path / "equation.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        _paragraph(page, 90, LOREM * 2, height=150)
        page.insert_text((140, 340), "1/n", fontsize=12)
        page.insert_text((340, 340), "x=+y", fontsize=12)
        page.insert_text((520, 340), "(13)", fontsize=12)
        _paragraph(page, 410, LOREM * 2, height=150)
        boxes = [tuple(b[:4]) for b in page.get_text("blocks") if 320 < b[1] < 350]
        doc.save(pdf)
    parsed = PyMuPDFParser().parse(pdf, tmp_path / "out", Config().parsing)
    equation = next(a for a in parsed.assets if a.kind == "equation" and a.label == "Eq. (13)")
    assert equation.bbox is not None
    assert equation.bbox[0] <= min(b[0] for b in boxes)
    assert equation.bbox[2] >= max(b[2] for b in boxes)
    assert (tmp_path / "out" / str(equation.path)).is_file()


def test_two_column_equations_stay_separate() -> None:
    blocks = []
    for x0, x1 in [(50, 280), (315, 545)]:
        bbox = (x0, 100.0, x1, 200.0)
        blocks.append(Block(1, bbox, [Line(LOREM, bbox, 10, False)]))
        bbox = (x0 + 20, 300.0, x0 + 80, 312.0)
        blocks.append(Block(1, bbox, [Line("x=+y", bbox, 10, False, 0.5)]))
    equations = [b for b in _group_equations(blocks, 10, 595) if b.kind == "equation"]
    assert len(equations) == 2
    assert all(b.bbox[2] - b.bbox[0] == 60 for b in equations)


RESULTS = {
    "equation": {"latex": r"\cdots + x", "symbols": []},
    "table": {"columns": ["Gain"], "rows": [["3.25"]], "units": {}, "footnotes": []},
    "chart": {"axes": {}, "legend": [], "series": []},
}


class Transport:
    def __init__(self, content: str) -> None:
        self.content = content
        self.bodies: list[dict[str, Any]] = []

    def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.bodies.append(body)
        return {"choices": [{"message": {"content": self.content}}]}


def _asset(conn: sqlite3.Connection, paths: Paths, mode: str) -> None:
    seed(conn)
    (paths.root / "parsed").mkdir(exist_ok=True)
    (paths.root / "parsed" / "crop.png").write_bytes(b"crop")
    conn.execute("INSERT INTO assets(id,parse_run_id,kind,label,page,bbox_json,path,quality) "
                 "VALUES ('asset','rp',?,'(13)',1,'[]','parsed/crop.png','verified')",
                 ("figure" if mode == "chart" else mode,))


@pytest.mark.parametrize("mode", RESULTS)
def test_cut_off_crop_schema_prompt_storage_and_cli(mode: str, conn: sqlite3.Connection, paths: Paths,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    _asset(conn, paths, mode)
    asset = conn.execute("SELECT * FROM assets WHERE id='asset'").fetchone()
    prompt = _prompt(mode, asset, [])[0]["content"]
    assert "only what is visible" in prompt
    assert r"\cdots" in prompt and "never complete or guess" in prompt.lower()
    assert "cut_off" in MODELS[mode].model_json_schema()["properties"]
    transport = Transport(json.dumps({**RESULTS[mode], "cut_off": True}))
    client = OpenAICompatibleClient("http://local/v1", "gemma", transport=transport)
    gateway = ModelGateway(conn, Config(), {"local_vision": client})
    analysis = analyze_asset(conn, gateway, paths, "asset", mode)  # type: ignore[arg-type]
    assert analysis.result["cut_off"] is True
    assert "cut_off_crop" in analysis.fidelity["flags"]
    assert asset_analyses(conn, "asset")[0].fidelity == analysis.fidelity
    _print_analysis(analysis)
    assert "cut_off_crop" in capsys.readouterr().out


@pytest.mark.parametrize("suffix", [" (ev_abcdef0123456789)", " [ev_abcdef0123456789, ev_12345678]",
                                    ", ev_abcdef0123456789 and ev_12345678", " (ev_abcdef0123456789),"])
def test_evidence_ids_removed_before_validation_and_storage(suffix: str) -> None:
    raw = "Gain is 3.25" + suffix + "."
    ids = ["ev_abcdef0123456789"]
    packet = EvidencePacket(items=[EvidenceItem(id=ids[0], paper_id="p", text="Gain is 3.25", page=1)])
    sentences, invalid = validate_sentences([SentenceDraft(text=raw, kind="reported_fact", evidence_ids=ids)], packet)
    assert sentences[0].flags == ["unquoted_fact"] and invalid == []
    assert sentences[0].text == "Gain is 3.25."
    assert sentences[0].evidence_ids == ids
    assert GapSentence(text=raw, evidence_ids=ids).text == "Gain is 3.25."
    assert ClaimDraft(text=raw, type="finding", polarity="positive", evidence_ids=ids).text == "Gain is 3.25."
    cell, flags = validate_cell(CellDraft(status="reported", value=3.25, note=raw, evidence_ids=ids),
                                MatrixColumn(key="gain", label="Gain", instruction="Gain", type="number"), packet)
    assert cell.note == "Gain is 3.25." and flags == []
    assert AuditDraft(verdict="supported", note=raw).note == "Gain is 3.25."


@pytest.mark.parametrize("mode, expected", [("chart", 4096), ("table", 4096), ("equation", 1024)])
@pytest.mark.parametrize("override", [None, "profile", "task"])
def test_asset_output_caps_match_plan_and_transport(mode: str, expected: int, override: str | None,
                                                   conn: sqlite3.Connection, paths: Paths) -> None:
    _asset(conn, paths, mode)
    cfg = Config()
    cfg.models.profiles["image"] = cfg.models.local.model_copy(update={"vision": True})
    cfg.models.tasks = {"asset_analyze": {"profile": "image", **(
        {"max_output_tokens": 5678} if override == "task" else {})}}  # type: ignore[dict-item]
    cfg = Config.model_validate(cfg.model_dump())
    if override == "profile":
        cfg.models.profiles["image"].max_output_tokens = 4567
    expected = 5678 if override == "task" else 4567 if override == "profile" else expected
    transport = Transport(json.dumps({**RESULTS[mode], "cut_off": False}))
    client = OpenAICompatibleClient("http://local/v1", "gemma", transport=transport)
    gateway = ModelGateway(conn, cfg, {"image": client})
    assert plan_asset(conn, gateway, "asset", mode).max_output_tokens == expected
    analyze_asset(conn, gateway, paths, "asset", mode)  # type: ignore[arg-type]
    assert transport.bodies[0]["max_tokens"] == expected
    if mode == "chart":
        assert "at most 25 points per series" in str(transport.bodies[0]["messages"])


@pytest.mark.parametrize("context", [None, 5000, 20000])
def test_page_rewrite_cap_fits_context_and_matches_transport(conn: sqlite3.Connection, context: int | None) -> None:
    cfg = Config()
    cfg.models.profiles["image"] = cfg.models.local.model_copy(update={"vision": True, "context_tokens": context})
    cfg.models.tasks = {"page_rewrite": {"profile": "image"}}  # type: ignore[dict-item]
    cfg = Config.model_validate(cfg.model_dump())
    transport = Transport("Transcription")
    engine = GatewayVision(ModelGateway(conn, cfg, {"image": OpenAICompatibleClient(
        "http://local/v1", "gemma", transport=transport)}))
    preview = engine.preview()
    expected = min(8192, context - preview.estimated_input_tokens) if context else 8192
    assert preview.max_output_tokens == expected and preview.blocked_reason is None
    engine.transcribe(b"png", "")
    assert transport.bodies[0]["max_tokens"] == expected


def test_claim_extraction_default_cap(conn: sqlite3.Connection) -> None:
    packet = EvidencePacket(items=[EvidenceItem(id="ev_12345678", paper_id="p", text="Gain 3.25", page=1)])
    assert ModelGateway(conn, Config()).preview("claim_extract", packet, []).max_output_tokens == 2048


def test_page_rewrite_preserves_equation_labels() -> None:
    prose = LOREM * 2
    page = prose + "\nx = 1 (8)\ny = 2\n(9)\nz = 3 (10)"
    assert not plausible(prose, page)
    assert not plausible(prose + r" $$x=1 \tag{10}$$", page)
    assert plausible(prose + r" $$x=1 \tag{8}$$ (9) $$z=3 \tag{10}$$", page)
    assert plausible("See equation (8) for details.", "See equation (8) for details.")


@pytest.mark.parametrize("tail", ["\n$$\nx = 1", "\n$$x=1$$\n$$y=2", "\n| x | y |\n|---|---|\n| 1 |",
                                  "\n| x | y |\n|---|---|\n| 1 | 2 |"])
def test_page_rewrite_rejects_truncated_math_or_table(tail: str) -> None:
    assert not plausible(LOREM + tail, LOREM)


def test_cached_rewrite_rechecked_with_specific_missing_equations_status(tmp_path: Path) -> None:
    pdf, out = tmp_path / "paper.pdf", tmp_path / "parsed"
    with pymupdf.open() as doc:
        page = doc.new_page()
        _paragraph(page, 90, LOREM, height=150)
        page.insert_text((200, 340), "x=+y (8)")
        page.insert_text((200, 380), "y=+z (9)")
        doc.save(pdf)
    parsed = PyMuPDFParser().parse(pdf, out, Config().parsing)
    (out / "canonical.json").write_text(parsed.model_dump_json())
    (out / "vision.json").write_text(json.dumps({"model": "fake-vision", "prompt_version": PROMPT_VERSION,
        "pages": {
        "1": {"raw": LOREM, "markdown": LOREM, "post_version": "6", "status": "ok"}}}))
    engine = FakeVision(lambda *_: pytest.fail("cached raw answer should be rechecked without a model call"))
    assert enhance_document(pdf, out, engine, 72) == (1, 0, 1)
    entry = json.loads((out / "vision.json").read_text())["pages"]["1"]
    assert entry["status"] == "rejected: missing equations (8), (9)"
    assert entry["post_version"] == POST_VERSION and POST_VERSION != "6"
