"""Synthetic PDF fixtures generated with PyMuPDF (no copyrighted content in the repo)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pymupdf
import pytest

from litforge.config import Config, Paths, load_config
from litforge.storage.db import open_db

LOREM = (
    "Neural mass models describe the average activity of large neuronal populations. "
    "We fit model parameters to electroencephalography recordings using simulation based "
    "inference. The posterior distribution captures uncertainty about synaptic gains and time "
    "constants. Results are compared against likelihood based estimation on held out data. "
)

HEADER = "Journal of Synthetic Tests, Vol. 1"


def _furniture(page: pymupdf.Page, number: int) -> None:
    page.insert_text((72, 30), HEADER, fontsize=8)
    page.insert_text((290, 815), str(number), fontsize=8)


def _paragraph(page: pymupdf.Page, y: float, text: str, x0: float = 72, x1: float = 523,
               height: float = 120) -> float:
    rect = pymupdf.Rect(x0, y, x1, y + height)
    page.insert_textbox(rect, text, fontsize=10, fontname="helv")
    return y + height + 10


def make_single_column(path: Path) -> None:
    doc = pymupdf.open()
    doc.set_metadata({"title": "", "author": "Ada Lovelace; Alan Turing"})
    # Page 1: title, abstract, introduction
    p = doc.new_page(width=595, height=842)
    _furniture(p, 1)
    p.insert_text((72, 90), "Inferring Neural Mass Parameters", fontsize=20, fontname="hebo")
    p.insert_text((72, 115), "Ada Lovelace and Alan Turing", fontsize=11)
    p.insert_text((72, 135), "doi:10.1234/synthetic.5678", fontsize=9)
    p.insert_text((72, 170), "Abstract", fontsize=13, fontname="hebo")
    y = _paragraph(p, 180, LOREM * 2, height=110)
    p.insert_text((72, y + 10), "1 Introduction", fontsize=13, fontname="hebo")
    y = _paragraph(p, y + 20, "PARA-INTRO-ONE. " + LOREM * 2, height=120)
    _paragraph(p, y, "PARA-INTRO-TWO. " + LOREM * 2, height=120)
    # Page 2: methods with equation, figure, table
    p = doc.new_page(width=595, height=842)
    _furniture(p, 2)
    p.insert_text((72, 80), "2 Methods", fontsize=13, fontname="hebo")
    y = _paragraph(p, 90, "PARA-METHODS. The Jansen Rit model is used. " + LOREM, height=90)
    p.insert_text((200, y + 10), "dy/dt = A a S(v) - 2 a y - a^2 x      (1)", fontsize=10)
    # Figure: vector drawing cluster then caption below
    fig_top = y + 40
    shape = p.new_shape()
    shape.draw_rect(pymupdf.Rect(150, fig_top, 450, fig_top + 160))
    shape.draw_line((160, fig_top + 150), (440, fig_top + 20))
    shape.draw_circle((300, fig_top + 80), 30)
    shape.finish(color=(0, 0, 1), width=1.5)
    shape.commit()
    cap_y = fig_top + 175
    p.insert_textbox(pymupdf.Rect(72, cap_y, 523, cap_y + 30),
                     "Figure 1: Posterior marginals of the synaptic gain parameters.", fontsize=9)
    # Table with ruling lines
    t_top = cap_y + 50
    p.insert_textbox(pymupdf.Rect(72, t_top, 523, t_top + 20),
                     "Table 1: Estimated parameters and errors.", fontsize=9)
    grid_top = t_top + 25
    rows, cols, cw, rh = 3, 3, 120, 20
    tbl = p.new_shape()
    for r in range(rows + 1):
        tbl.draw_line((100, grid_top + r * rh), (100 + cols * cw, grid_top + r * rh))
    for c in range(cols + 1):
        tbl.draw_line((100 + c * cw, grid_top), (100 + c * cw, grid_top + rows * rh))
    tbl.finish(color=(0, 0, 0), width=0.8)
    tbl.commit()
    cells = [["Parameter", "Estimate", "Error"], ["gain A", "3.25", "0.10"], ["gain B", "22.0", "1.50"]]
    for r, row in enumerate(cells):
        for c, val in enumerate(row):
            p.insert_text((105 + c * cw, grid_top + r * rh + 14), val, fontsize=9)
    # Page 3: results, references
    p = doc.new_page(width=595, height=842)
    _furniture(p, 3)
    p.insert_text((72, 80), "3 Results", fontsize=13, fontname="hebo")
    y = _paragraph(p, 90, "PARA-RESULTS. The hippocampal oscillation zebrafinch marker. " + LOREM, height=90)
    p.insert_text((72, y + 10), "References", fontsize=13, fontname="hebo")
    _paragraph(p, y + 20, "[1] Jansen BH, Rit VG. Electroencephalogram and visual evoked potential "
               "generation. Biol Cybern 1995. REFERENCE-ONLY-TOKEN.", height=60)
    doc.save(path)
    doc.close()


def make_two_column(path: Path) -> None:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 70), "A Two Column Study of Reading Order", fontsize=18, fontname="hebo")
    left_x, right_x, width = 50, 310, 235
    y = 110
    for i in range(1, 5):
        _paragraph(p, y, f"LEFTCOL{i} " + LOREM, x0=left_x, x1=left_x + width, height=150)
        _paragraph(p, y, f"RIGHTCOL{i} " + LOREM, x0=right_x, x1=right_x + width, height=150)
        y += 165
    doc.save(path)
    doc.close()


def make_image_only(path: Path) -> None:
    doc = pymupdf.open()
    for _ in range(2):
        p = doc.new_page(width=595, height=842)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 500), False)
        pix.set_rect(pix.irect, (200, 200, 200))
        p.insert_image(pymupdf.Rect(80, 80, 480, 580), pixmap=pix)
    doc.save(path)
    doc.close()


def make_encrypted(path: Path) -> None:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "secret", fontsize=12)
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    doc.close()


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus")
    make_single_column(root / "single_column.pdf")
    make_two_column(root / "two_column.pdf")
    make_image_only(root / "image_only.pdf")
    make_encrypted(root / "encrypted.pdf")
    (root / "corrupt.pdf").write_bytes(b"%PDF-1.7\n1 0 obj << /Broken >> garbage")
    (root / "not_a_pdf.pdf").write_bytes(b"hello, I am a text file")
    sub = root / "nested"
    sub.mkdir()
    shutil.copy(root / "single_column.pdf", sub / "same_bytes_other_name.pdf")
    return root


@pytest.fixture(autouse=True)
def _exports_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default exports go to ~/Downloads/LitForge; tests must never write there. Settings from the
    developer's shell (a data directory, a running Ollama) must not leak into tests either."""
    for name in [n for n in os.environ if n.startswith("LITFORGE_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("LITFORGE_EXPORT_ROOT", str(tmp_path / "exports"))


@pytest.fixture()
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "data")
    p.ensure()
    return p


@pytest.fixture()
def config() -> Config:
    return load_config()


@pytest.fixture()
def conn(paths: Paths):  # type: ignore[no-untyped-def]
    c = open_db(paths.db)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_system_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never consult the developer's actual keychain. Secret tests inject their own backend."""
    from litforge.llm import secrets
    def unavailable(args: list[str], value: str | None = None) -> str:
        raise secrets.SecretMissing("test keychain is empty")
    monkeypatch.setattr(secrets, "_run", unavailable)
    # Windows tests inject Credentials/DLL explicitly; default resolution is offline everywhere.
    monkeypatch.setattr(secrets.KeyStore, "_windows", lambda self: self.credentials or EmptyCredentials())


class EmptyCredentials:
    def read(self, name: str) -> None:
        return None
    def write(self, name: str, value: str) -> None:
        raise AssertionError("Tests must inject a fake credential layer")
    def delete(self, name: str) -> None:
        raise AssertionError("Tests must inject a fake credential layer")


@pytest.fixture(autouse=True)
def no_real_nemotron(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never see the developer's GPU or start a real Nemotron Parse install or download; tests that need
    Nemotron patch these back on and inject a fake engine."""
    from litforge import tools
    from litforge.parsing import nemotron

    monkeypatch.setattr(tools, "_has_nvidia_gpu", lambda root: False)
    monkeypatch.setattr(nemotron, "gpu_free_gb", lambda: None)
    monkeypatch.setattr(nemotron, "download_model", lambda paths, progress=None: (_ for _ in ()).throw(
        RuntimeError("tests must not download Nemotron Parse")))
