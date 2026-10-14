"""Regression tests for layout heuristics found on real arXiv papers (see scripts/benchmark_parsers.py)."""

from typing import Any

from litforge.parsing.pymupdf_parser import (
    Block,
    Line,
    PyMuPDFParser,
    _column_groups,
    _merge_section_numbers,
    _segment_spans,
)


def _span(text: str, x0: float, x1: float, size: float = 10.0, bold: bool = False) -> dict[str, Any]:
    return {"text": text, "bbox": (x0, 100.0, x1, 110.0), "size": size, "flags": 16 if bold else 0, "font": "Times"}


def _block(text: str, bbox: tuple[float, float, float, float], size: float = 10.0, bold: bool = False) -> Block:
    return Block(page=2, bbox=bbox, lines=[Line(text, bbox, size, bold)])


def test_wide_gap_splits_line_but_equation_label_stays() -> None:
    # Neural ODE: heading merged with figure labels on the same line.
    segs = _segment_spans([_span("Introduction", 126, 191), _span("Residual Network", 330, 400)])
    assert [[s["text"] for s in seg] for seg in segs] == [["Introduction"], ["Residual Network"]]
    # Display equation with its number far to the right is kept together.
    segs = _segment_spans([_span("x = f(z)", 200, 260), _span("(3)", 500, 512)])
    assert len(segs) == 1


def test_column_groups_separate_non_overlapping_lines() -> None:
    lines = [Line("Introduction", (126, 386, 191, 398), 12, True),
             Line("Residual Network", (330, 395, 400, 405), 8, False),
             Line("ODE Network", (450, 395, 500, 405), 8, False)]
    assert [[ln.text for ln in g] for g in _column_groups(lines)] == [
        ["Introduction"], ["Residual Network"], ["ODE Network"]]


def test_bare_section_number_merges_with_heading() -> None:
    merged = _merge_section_numbers([_block("2", (105, 451, 111, 465)), _block("ALGORITHM", (123, 451, 185, 465))])
    assert [b.text for b in merged] == ["2 ALGORITHM"]
    merged = _merge_section_numbers([_block("A", (76, 230, 86, 242)), _block("Detailed models", (99, 230, 218, 242))])
    assert [b.text for b in merged] == ["A Detailed models"]


def test_heading_classification_rules() -> None:
    def kind(text: str, size: float = 10.0, bold: bool = False) -> str:
        block = _block(text, (72, 100, 300, 112), size, bold)
        PyMuPDFParser._classify(block, body_size=10.0)
        return block.kind

    assert kind("2 ALGORITHM") == "heading"  # small caps, numbered (Adam)
    assert kind("POWER") == "text"  # bare caps table label (MAF)
    assert kind("Identity vs. Projection Shortcuts. We have shown", bold=True) == "text"  # run-in (ResNet)
    assert kind("Lotka–Volterra population model [84]. This is a", bold=True) == "text"  # run-in (SNL)
    assert kind("Sθ = (s1, s2)", bold=True) == "text"  # math, not a heading
    assert kind("3.2 Attention", size=12) == "heading"
    assert kind("Table 3 shows that all options help") == "text"
    assert kind("Table 3. Error rates on ImageNet") == "caption"
