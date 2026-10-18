"""Optional structural parser built on Docling (install with `uv sync --extra docling`).

Maps Docling's document items onto the canonical model with page provenance. This adapter
is untested until Docling is installed; failures surface as parse errors, never as empty
successes.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any

from litforge.config import ParsingConfig
from litforge.parsing.base import (
    Asset,
    BBox,
    PageInfo,
    Paragraph,
    ParagraphKind,
    ParsedDocument,
    Section,
    section_kind,
)


def docling_available() -> bool:
    try:
        metadata.version("docling")
    except metadata.PackageNotFoundError:
        return False
    return True


def _bbox(prov: Any, page_height: float) -> BBox | None:
    """Convert a Docling bbox (often bottom-left origin) to top-left PDF points."""
    try:
        box = prov.bbox.to_top_left_origin(page_height)
        return (box.l, box.t, box.r, box.b)
    except Exception:  # noqa: BLE001
        return None


class DoclingParser:
    name = "docling"

    @property
    def version(self) -> str:
        return metadata.version("docling")

    def parse(self, pdf_path: Path, out_dir: Path, config: ParsingConfig) -> ParsedDocument:
        from docling.document_converter import DocumentConverter

        result = DocumentConverter().convert(str(pdf_path))
        ddoc = result.document
        heights = {no: page.size.height for no, page in ddoc.pages.items()}
        pages = [
            PageInfo(index=no, label=None, width=page.size.width, height=page.size.height,
                     char_count=0, image_count=0)
            for no, page in sorted(ddoc.pages.items())
        ]
        sections: list[Section] = [Section(heading=None, kind="front", level=1)]
        assets: list[Asset] = []
        for item, level in ddoc.iterate_items():
            label = str(getattr(item, "label", "")).lower()
            prov = item.prov[0] if getattr(item, "prov", None) else None
            page_no = prov.page_no if prov else 1
            bbox = _bbox(prov, heights.get(page_no, 0)) if prov else None
            if label in ("page_header", "page_footer"):
                continue
            if label == "section_header":  # the title stays a front-matter paragraph
                text = item.text.strip()
                sections.append(Section(heading=text, kind=section_kind(text), level=max(1, level)))
                continue
            if label == "table":
                caption = item.caption_text(ddoc) or None
                markdown = item.export_to_markdown(ddoc)
                assets.append(Asset(kind="table", label=None, caption=caption, page=page_no,
                                    bbox=bbox, data={"markdown": markdown}, quality="uncertain"))
                sections[-1].paragraphs.append(Paragraph(
                    text=f"{caption or 'Table'}\n\n{markdown}", kind="caption", page=page_no, bbox=bbox))
                continue
            if label == "picture":
                caption = item.caption_text(ddoc) or None
                assets.append(Asset(kind="figure", label=None, caption=caption, page=page_no,
                                    bbox=bbox, quality="uncertain"))
                continue
            text = getattr(item, "text", "").strip()
            if not text:
                continue
            kind_p: ParagraphKind = "equation" if label == "formula" else "caption" if label == "caption" else "text"
            sections[-1].paragraphs.append(Paragraph(text=text, kind=kind_p, page=page_no, bbox=bbox))
        for section in sections:
            for para in section.paragraphs:
                if 1 <= para.page <= len(pages):
                    pages[para.page - 1].char_count += len(para.text)
        return ParsedDocument(
            parser=self.name,
            parser_version=self.version,
            page_count=len(pages),
            pages=pages,
            sections=[s for s in sections if s.paragraphs or s.heading],
            assets=assets,
        )
