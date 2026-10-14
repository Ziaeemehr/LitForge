"""Parser-independent canonical document model and the parser interface."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from litforge.config import ParsingConfig

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points, top-left origin

SectionKind = Literal["front", "abstract", "body", "references", "appendix"]
ParagraphKind = Literal["text", "caption", "equation"]
AssetKind = Literal["figure", "table", "equation"]
AssetQuality = Literal["verified", "uncertain", "failed"]


class Paragraph(BaseModel):
    text: str
    kind: ParagraphKind = "text"
    page: int  # 1-based PDF page index
    bbox: BBox | None = None


class Section(BaseModel):
    heading: str | None
    kind: SectionKind
    level: int = 1
    paragraphs: list[Paragraph] = Field(default_factory=list)

    @property
    def page_start(self) -> int | None:
        return min((p.page for p in self.paragraphs), default=None)

    @property
    def page_end(self) -> int | None:
        return max((p.page for p in self.paragraphs), default=None)


class Asset(BaseModel):
    kind: AssetKind
    label: str | None
    caption: str | None
    page: int
    bbox: BBox | None
    path: str | None = None  # relative to the parse output directory
    data: dict[str, object] | None = None
    quality: AssetQuality = "uncertain"


class PageInfo(BaseModel):
    index: int  # 1-based
    label: str | None  # printed page label, when the PDF defines one
    width: float
    height: float
    char_count: int
    image_count: int
    column_count: int = 1
    line_count: int = 0
    short_line_count: int = 0
    replacement_chars: int = 0


class ParsedDocument(BaseModel):
    parser: str
    parser_version: str
    page_count: int
    pages: list[PageInfo]
    sections: list[Section]
    assets: list[Asset] = Field(default_factory=list)
    furniture: list[str] = Field(default_factory=list)  # removed running headers/footers

    def full_text(self) -> str:
        return "\n\n".join(p.text for s in self.sections for p in s.paragraphs)


class Parser(Protocol):
    name: str

    @property
    def version(self) -> str: ...

    def parse(self, pdf_path: Path, out_dir: Path, config: ParsingConfig) -> ParsedDocument:
        """Parse `pdf_path`; write any assets under `out_dir`; return the canonical document."""
        ...
