from typing import Any

from litforge.config import RouterThresholds
from litforge.parsing.quality import QualityMetrics
from litforge.parsing.router import decide

T = RouterThresholds()


def _q(**overrides: Any) -> QualityMetrics:
    base: dict[str, Any] = dict(
        page_count=10, total_chars=30000, chars_per_page=[3000] * 10, sparse_pages=[],
        sparse_pages_with_images=[], blank_pages=[], replacement_char_ratio=0.0,
        fragmented_line_ratio=0.05, two_column_pages=10, section_count=6, has_abstract=True,
        has_references=True, repeated_furniture=2, table_count=1, figure_count=3,
        figures_without_region=0, paragraph_count=80,
    )
    base.update(overrides)
    return QualityMetrics(**base)


def test_clean_paper_stays_on_fast_path() -> None:
    d = decide(_q(), T)
    assert d.route == "pymupdf" and d.flags == []


def test_empty_with_images_needs_ocr() -> None:
    d = decide(_q(total_chars=0, sparse_pages=[1, 2], sparse_pages_with_images=[1, 2]), T)
    assert d.route == "needs_ocr" and d.ocr_pages == [1, 2]


def test_empty_without_images_needs_review() -> None:
    assert decide(_q(total_chars=0), T).route == "needs_review"


def test_mostly_scanned_needs_ocr_but_few_scanned_pages_only_flag() -> None:
    assert decide(_q(sparse_pages_with_images=list(range(1, 8))), T).route == "needs_ocr"
    d = decide(_q(sparse_pages_with_images=[9]), T)
    assert d.route == "pymupdf" and "ocr_candidate_pages:9" in d.flags


def test_garbled_text_or_many_tables_needs_docling() -> None:
    assert decide(_q(replacement_char_ratio=0.05), T).route == "needs_docling"
    assert decide(_q(fragmented_line_ratio=0.6), T).route == "needs_docling"
    assert decide(_q(table_count=12), T).route == "needs_docling"


def test_missing_structure_is_flagged_not_blocking() -> None:
    d = decide(_q(section_count=0, has_references=False), T)
    assert d.route == "pymupdf"
    assert {"no_sections_detected", "no_references_detected"} <= set(d.flags)
