"""Measurable extraction-quality signals computed from a parsed document."""

from __future__ import annotations

from pydantic import BaseModel

from litforge.config import RouterThresholds
from litforge.parsing.base import ParsedDocument


class QualityMetrics(BaseModel):
    page_count: int
    total_chars: int
    chars_per_page: list[int]
    sparse_pages: list[int]  # 1-based pages below the text threshold
    sparse_pages_with_images: list[int]
    blank_pages: list[int]
    replacement_char_ratio: float
    fragmented_line_ratio: float
    two_column_pages: int
    section_count: int
    has_abstract: bool
    has_references: bool
    repeated_furniture: int
    table_count: int
    figure_count: int
    figures_without_region: int
    paragraph_count: int


def compute_quality(doc: ParsedDocument, thresholds: RouterThresholds) -> QualityMetrics:
    chars = [p.char_count for p in doc.pages]
    total = sum(chars)
    sparse = [p.index for p in doc.pages if p.char_count < thresholds.min_chars_per_page]
    sparse_img = [p.index for p in doc.pages if p.index in sparse and p.image_count > 0]
    blank = [p.index for p in doc.pages if p.char_count == 0 and p.image_count == 0]
    lines = sum(p.line_count for p in doc.pages)
    short = sum(p.short_line_count for p in doc.pages)
    bad = sum(p.replacement_chars for p in doc.pages)
    figures = [a for a in doc.assets if a.kind == "figure"]
    return QualityMetrics(
        page_count=doc.page_count,
        total_chars=total,
        chars_per_page=chars,
        sparse_pages=sparse,
        sparse_pages_with_images=sparse_img,
        blank_pages=blank,
        replacement_char_ratio=round(bad / total, 5) if total else 0.0,
        fragmented_line_ratio=round(short / lines, 4) if lines else 0.0,
        two_column_pages=sum(1 for p in doc.pages if p.column_count == 2),
        section_count=sum(1 for s in doc.sections if s.heading),
        has_abstract=any(s.kind == "abstract" for s in doc.sections),
        has_references=any(s.kind == "references" for s in doc.sections),
        repeated_furniture=len(doc.furniture),
        table_count=sum(1 for a in doc.assets if a.kind == "table" and a.bbox is not None),
        figure_count=len(figures),
        figures_without_region=sum(1 for a in figures if a.bbox is None),
        paragraph_count=sum(len(s.paragraphs) for s in doc.sections),
    )
