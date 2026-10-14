"""Route a fast-path parse to: keep it, send to Docling, or send pages to OCR."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from litforge.config import RouterThresholds
from litforge.parsing.quality import QualityMetrics

Route = Literal["pymupdf", "needs_docling", "needs_ocr", "needs_review"]


class RouteDecision(BaseModel):
    route: Route
    reasons: list[str]
    ocr_pages: list[int] = []
    flags: list[str] = []  # non-blocking quality notes


def decide(q: QualityMetrics, t: RouterThresholds) -> RouteDecision:
    flags: list[str] = []
    if not q.section_count:
        flags.append("no_sections_detected")
    if not q.has_references:
        flags.append("no_references_detected")
    if q.figures_without_region:
        flags.append("figure_region_missing")

    if q.total_chars == 0:
        if q.sparse_pages_with_images:
            return RouteDecision(
                route="needs_ocr", reasons=["no extractable text; pages contain images"],
                ocr_pages=q.sparse_pages_with_images, flags=flags,
            )
        return RouteDecision(route="needs_review", reasons=["empty extraction"], flags=flags)

    reasons: list[str] = []
    non_blank = q.page_count - len(q.blank_pages)
    sparse_ratio = len(q.sparse_pages_with_images) / non_blank if non_blank else 0.0
    if sparse_ratio > t.max_sparse_page_ratio:
        return RouteDecision(
            route="needs_ocr",
            reasons=[f"{len(q.sparse_pages_with_images)}/{non_blank} pages are image-only or text-sparse"],
            ocr_pages=q.sparse_pages_with_images,
            flags=flags,
        )
    if q.sparse_pages_with_images:
        flags.append(f"ocr_candidate_pages:{','.join(map(str, q.sparse_pages_with_images))}")

    if q.replacement_char_ratio > t.max_replacement_char_ratio:
        reasons.append(f"replacement/control char ratio {q.replacement_char_ratio:.4f}")
    if q.fragmented_line_ratio > t.max_fragmented_line_ratio:
        reasons.append(f"fragmented line ratio {q.fragmented_line_ratio:.2f}")
    if q.table_count > t.max_table_count:
        reasons.append(f"{q.table_count} tables detected")
    if reasons:
        return RouteDecision(
            route="needs_docling", reasons=reasons,
            ocr_pages=q.sparse_pages_with_images, flags=flags,
        )
    return RouteDecision(route="pymupdf", reasons=["text-rich, quality checks passed"],
                         ocr_pages=q.sparse_pages_with_images, flags=flags)
