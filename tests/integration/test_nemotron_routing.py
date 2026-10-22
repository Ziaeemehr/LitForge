import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from litforge.config import Config, Paths
from litforge.ingest import ingest_paths
from litforge.parsing import nemotron, run
from litforge.parsing.run import artifacts_for_project, parse_artifact, structural_parsers
from litforge.parsing.vision import VISION, FakeVision, RoutedVision, enhance_project, page_engine
from litforge.projects import create_project

PERSIAN = "در این بخش مدل جرم عصبی را معرفی می‌کنیم و توزیع پسین را برآورد می‌کنیم. " * 3


@pytest.fixture()
def project(conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path) -> str:
    pid = create_project(conn, paths, "route")
    ingest_paths(conn, paths, [corpus / "single_column.pdf"], pid)
    for art in artifacts_for_project(conn, pid):
        parse_artifact(conn, paths, config, art)
    return pid


class Reader(FakeVision):
    """Stands in for Nemotron: answers raw output that `render` turns into Markdown."""

    def render(self, raw: str) -> str:
        return raw.removeprefix("RAW:")


def _english(png: bytes, text: str) -> str:
    return "RAW:" + text + "\n\n$$\n\\frac{dy}{dt} = A a S(v) \\tag{1}\n$$"


def test_routed_vision_sends_english_pages_to_nemotron_and_records_the_engine(
    conn: sqlite3.Connection, paths: Paths, config: Config, project: str
) -> None:
    reader, model = Reader(_english, name="nemo"), FakeVision(lambda png, text: text, name="qwen")
    report = enhance_project(conn, paths, config, project, RoutedVision(reader, model))
    assert report.pages_improved >= 1 and reader.calls == report.pages_improved and model.calls == 0
    saved = json.loads(next(paths.parsed.rglob(VISION)).read_text())
    pages = saved["pages"].values()
    assert saved["model"] == "nemo+qwen" and all(p["engine"] == "nemo" for p in pages)
    assert all("RAW:" not in (p["markdown"] or "") and "\\tag{1}" in p["markdown"] for p in pages)


def test_routed_vision_sends_persian_pages_and_persian_scans_to_the_model() -> None:
    reader, model = Reader(lambda png, text: "RAW:" + PERSIAN, name="nemo"), FakeVision(lambda p, t: "ok", "qwen")
    routed = RoutedVision(reader, model)
    assert routed.transcribe(b"", PERSIAN) == "ok" and reader.calls == 0 and routed.last is model
    # A scan has no text to tell by: Nemotron reads it first, and a Persian answer goes to the model.
    assert routed.transcribe(b"", "") == "ok" and reader.calls == 1 and model.calls == 2
    english = RoutedVision(Reader(lambda png, text: "RAW:plain English words here " * 5, name="nemo"), model)
    assert english.transcribe(b"", "").startswith("RAW:") and english.last is english.reader


def test_without_a_vision_model_persian_pages_keep_their_text() -> None:
    routed = RoutedVision(Reader(_english, name="nemo"), None)
    assert routed.transcribe(b"", PERSIAN) == "" and routed.last is None and routed.name == "nemo+none"


def test_page_engine_follows_the_setting(paths: Paths, config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeVision(lambda png, text: text, name="qwen")
    assert page_engine(paths, config, lambda: model) is model  # Nemotron not set up
    monkeypatch.setattr(nemotron, "available", lambda paths: True)
    monkeypatch.setattr(nemotron, "NemotronEngine", lambda paths: Reader(_english, name="nemo"))
    assert page_engine(paths, config, lambda: model) is model  # set up, but no GPU with room
    monkeypatch.setattr(nemotron, "gpu_ready", lambda: True)
    routed = page_engine(paths, config, lambda: model)
    assert isinstance(routed, RoutedVision) and routed.model is model

    def missing() -> FakeVision:
        raise RuntimeError("Ollama is not running")

    assert isinstance(none := page_engine(paths, config, missing), RoutedVision) and none.model is None
    config.vision.engine = "vision_model"
    assert page_engine(paths, config, lambda: model) is model
    config.vision.engine = "nemotron"
    assert isinstance(page_engine(paths, config, None), Reader)
    monkeypatch.setattr(nemotron, "available", lambda paths: False)
    with pytest.raises(ValueError, match="not set up"):
        page_engine(paths, config, lambda: model)


def test_structural_parsers_prefer_nemotron_on_a_gpu_for_english(
    paths: Paths, config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pdf = tmp_path / "x.pdf"
    monkeypatch.setattr(run, "find_command", lambda paths, cfg: "mineru-kit")
    monkeypatch.setattr(run, "docling_available", lambda: True)
    monkeypatch.setattr(nemotron, "available", lambda paths: True)
    monkeypatch.setattr(run, "persian_pdf", lambda path: False)

    def names(status: str = "needs_docling") -> list[str]:
        return [p.name for p in structural_parsers(paths, config, pdf, status)]

    assert names() == ["mineru", "docling"]  # no GPU: MinerU first
    monkeypatch.setattr(nemotron, "gpu_ready", lambda: True)
    assert names() == ["nemotron", "mineru", "docling"]
    assert names("needs_ocr") == ["nemotron"]  # only Nemotron reads a document without a text layer
    monkeypatch.setattr(run, "persian_pdf", lambda path: True)
    assert names() == ["mineru", "docling"]  # Persian: never Nemotron
    config.parsing.complex_fallback = "nemotron"
    monkeypatch.setattr(nemotron, "gpu_ready", lambda: False)
    assert names() == ["nemotron"]  # chosen by hand: also without a GPU
    config.parsing.complex_fallback = "mineru"
    assert names() == ["mineru"]


def test_a_failed_structural_parser_falls_through_to_the_next(
    conn: sqlite3.Connection, paths: Paths, config: Config, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litforge.parsing.pymupdf_parser import PyMuPDFParser
    from litforge.parsing.router import RouteDecision

    class Broken:
        name, version = "nemotron", "test"

        def parse(self, pdf_path: Path, out_dir: Path, cfg: object) -> None:
            raise RuntimeError("Nemotron returned Persian text")

    class Second(PyMuPDFParser):
        name = "mineru"

    pid = create_project(conn, paths, "chain")
    ingest_paths(conn, paths, [corpus / "single_column.pdf"], pid)
    art = artifacts_for_project(conn, pid)[0]
    monkeypatch.setattr(run, "decide", lambda q, t: RouteDecision(route="needs_docling", reasons=["test"]))
    monkeypatch.setattr(run, "structural_parsers", lambda *a: [Broken(), Second()])
    result = parse_artifact(conn, paths, config, art)
    assert result.parser == "mineru" and result.message is None


def test_engine_choices_are_saved_to_the_data_config(paths: Paths, config: Config) -> None:
    from litforge.ui.server import App

    app = App(paths, config)
    assert app.save_engines({"parse": "mineru", "page": "vision_model"}) == {"parse": "mineru", "page": "vision_model"}
    saved = yaml.safe_load((paths.root / "config.yaml").read_text())
    assert saved["parsing"]["complex_fallback"] == "mineru" and saved["vision"]["engine"] == "vision_model"
    assert app.config.vision.engine == "vision_model"
    status = app.optional_tools_status()
    assert status["engines"] == {"parse": "mineru", "page": "vision_model"}
    assert status["nemotron"]["offerable"] is False and "NVIDIA GPU" in status["nemotron"]["reason"]
    with pytest.raises(ValueError):
        app.save_engines({"page": "cloud"})
