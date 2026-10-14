from pathlib import Path

import pymupdf
from conftest import make_single_column

from litforge.config import load_config
from litforge.parsing.formulas import Formula, assemble_page, plausible_latex, tidy_latex
from litforge.parsing.pymupdf_parser import PyMuPDFParser

DPI = 100
SCALE = DPI / 72


def _px(rect: pymupdf.Rect) -> tuple[float, float, float, float]:
    return (rect.x0 * SCALE, rect.y0 * SCALE, rect.x1 * SCALE, rect.y1 * SCALE)


def test_assemble_page_puts_formulas_in_place_of_their_garbled_text(tmp_path: Path) -> None:
    make_single_column(tmp_path / "s.pdf")
    doc = PyMuPDFParser().parse(tmp_path / "s.pdf", tmp_path / "out", load_config().parsing)
    with pymupdf.open(tmp_path / "s.pdf") as pdf:
        page = pdf[1]
        eq = page.search_for("dy/dt = A a S(v) - 2 a y - a^2 x")[0]
        word = page.search_for("Jansen")[0]
        formulas = [
            Formula(box=_px(eq), kind="isolated", latex=r"\frac{dy}{dt}=A a S(v)-2 a y-a^{2} x"),
            Formula(box=_px(word), kind="embedding", latex=r"\mathcal{J}"),
        ]
        md = assemble_page(page, doc, 2, formulas, DPI)
    assert "$$\n\\frac{dy}{dt}=A a S(v)-2 a y-a^{2} x \\tag{1}\n$$" in md  # the number becomes its tag
    assert "dy/dt" not in md and "(1)" not in md
    assert "The $\\mathcal{J}$ Rit model is used." in md  # inline formula where its word was
    assert md.startswith("## 2 Methods\n")
    assert md.index("PARA-METHODS") < md.index("\\frac{dy}") < md.index("Figure 1: Posterior")
    assert "[Figure]\nFigure 1: Posterior marginals" in md  # the figure crop goes before its caption


def test_assemble_page_keeps_paragraphs_without_formulas_word_for_word(tmp_path: Path) -> None:
    make_single_column(tmp_path / "s.pdf")
    doc = PyMuPDFParser().parse(tmp_path / "s.pdf", tmp_path / "out", load_config().parsing)
    para = next(p for s in doc.sections for p in s.paragraphs if p.page == 2 and p.text.startswith("PARA-METHODS"))
    with pymupdf.open(tmp_path / "s.pdf") as pdf:
        md = assemble_page(pdf[1], doc, 2, [], DPI)
    assert para.text in md


def test_tidy_latex_undoes_spaced_out_names() -> None:
    spaced = r"\operatorname{e x p} \left[-t \right]+I_{\mathrm{e x t}}"
    assert tidy_latex(spaced) == r"\exp \left[-t \right]+I_{\mathrm{ext}}"
    assert tidy_latex(r"\operatorname{s i g n} ( x )") == r"\operatorname{sign} ( x )"
    assert tidy_latex(r"\begin{aligned} {x=1} \\ \end{aligned}") == r"\begin{aligned} {x=1} \end{aligned}"


def _scan_like_page(path: Path) -> None:
    """A display equation whose text layer is offset from the formula box, as in OCR'd scans, its number
    on the line below, and two paragraphs whose boxes overlap."""
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 100), "First paragraph ends with the constant A then reads:", fontsize=10)
    p.insert_text((90, 130), "A=", fontsize=10)  # left of where the detector puts the formula box
    p.insert_text((130, 124), "1", fontsize=10)
    p.insert_text((115, 136), "exp [-t/r1] - exp [-t/r2]", fontsize=10)
    p.insert_text((500, 148), "(2.5)", fontsize=10)  # the number, a line below
    p.insert_text((72, 170), "Second paragraph says the rest.", fontsize=10)
    doc.save(path)


def test_assemble_page_drops_fragments_beside_a_display_and_writes_each_line_once(tmp_path: Path) -> None:
    _scan_like_page(tmp_path / "scan.pdf")
    doc = PyMuPDFParser().parse(tmp_path / "scan.pdf", tmp_path / "out", load_config().parsing)
    for section in doc.sections:  # make the paragraph boxes overlap, as the parser sometimes does
        for para in section.paragraphs:
            if para.bbox:
                para.bbox = (para.bbox[0], para.bbox[1] - 15, para.bbox[2], para.bbox[3] + 15)
    box = pymupdf.Rect(105, 114, 260, 140)
    with pymupdf.open(tmp_path / "scan.pdf") as pdf:
        md = assemble_page(pdf[0], doc, 1, [Formula(box=_px(box), kind="isolated", latex=r"A=\frac{1}{e}")], DPI)
    assert "$$\nA=\\frac{1}{e} \\tag{2.5}\n$$" in md
    assert "A=\n" not in md and "exp [" not in md and "(2.5)" not in md.replace("\\tag{2.5}", "")
    assert md.count("First paragraph ends") == 1 and md.count("Second paragraph says") == 1
    assert md.index("First paragraph") < md.index("\\frac{1}{e}") < md.index("Second paragraph")


def test_assemble_page_leaves_the_other_column_beside_a_display_alone(tmp_path: Path) -> None:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    for i in range(6):
        p.insert_text((54, 100 + 12 * i), f"Left column line {i} keeps every word here.", fontsize=9)
    p.insert_text((330, 100), "Right column text above.", fontsize=9)
    p.insert_text((340, 130), "H(n) = e - e / a - 1", fontsize=9)
    doc.save(tmp_path / "two.pdf")
    parsed = PyMuPDFParser().parse(tmp_path / "two.pdf", tmp_path / "out", load_config().parsing)
    box = pymupdf.Rect(331, 112, 530, 150)
    with pymupdf.open(tmp_path / "two.pdf") as pdf:
        md = assemble_page(pdf[0], parsed, 1, [Formula(box=_px(box), kind="isolated", latex="H(n)")], DPI)
    for i in range(6):
        assert f"Left column line {i} keeps every word here." in md
    assert "$$\nH(n)\n$$" in md and "e - e" not in md


def test_inline_formula_lands_between_its_words_when_one_visual_line_is_two_text_lines(tmp_path: Path) -> None:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((54, 100), "Paragraph starts here and the line goes on to the edge of the column.", fontsize=9)
    p.insert_text((54, 112), "voltage.", fontsize=9)  # a separate text line, same height as the next one
    p.insert_text((96, 112), "We set xth = 1 and in our work.", fontsize=9)
    doc.save(tmp_path / "split.pdf")
    parsed = PyMuPDFParser().parse(tmp_path / "split.pdf", tmp_path / "out", load_config().parsing)
    with pymupdf.open(tmp_path / "split.pdf") as pdf:
        page = pdf[0]
        box = page.search_for("xth = 1")[0]
        md = assemble_page(page, parsed, 1, [Formula(box=_px(box), kind="embedding", latex="x_{th}=1")], DPI)
    assert "voltage. We set $x_{th}=1$ and in our work." in md


def test_a_parser_fragment_of_an_inline_formula_does_not_survive(tmp_path: Path) -> None:
    make_single_column(tmp_path / "s.pdf")
    doc = PyMuPDFParser().parse(tmp_path / "s.pdf", tmp_path / "out", load_config().parsing)
    with pymupdf.open(tmp_path / "s.pdf") as pdf:
        page = pdf[1]
        eq = page.search_for("dy/dt = A a S(v) - 2 a y - a^2 x")[0]
        # Read as an inline formula: the parser's equation paragraph must not come back as raw text.
        md = assemble_page(page, doc, 2, [Formula(box=_px(eq), kind="embedding", latex="y'")], DPI)
    assert "dy/dt" not in md and "```text" not in md and "$y'$" in md


def test_plausible_latex_rejects_runaway_and_broken_recognitions() -> None:
    assert plausible_latex(r"\frac{d V}{d t}=-\frac{V}{\tau_{0}}+I_{\mathrm{ext}}")
    assert not plausible_latex(r"\frac{\d t} {\d t} \int_{\d t}^{\d t} \frac{\d t} {\d t} \int_{\d t}^{\d t}")
    assert not plausible_latex(r"x + + + + + + + + y")  # the same token over and over
    assert plausible_latex(r"\begin{aligned} a & =1 , \quad\quad\, \, \, \, \, \, b=2 \end{aligned}")  # spacing
    assert not plausible_latex(r"B {\verb+} x")
    assert not plausible_latex(r"\left( x")  # unbalanced
    assert not plausible_latex("")
