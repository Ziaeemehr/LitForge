"""Heuristic bibliographic metadata from the PDF itself.

Every field records its source. Nothing here is verified against an external registry;
DOIs are marked `unverified`, and fields that cannot be found stay `None` (no guessing).
"""

from __future__ import annotations

import re
from typing import Any

import pymupdf
from pydantic import BaseModel

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
ABSTRACT_START_RE = re.compile(r"\babstract\b[\s.:—–\-]*", re.IGNORECASE)
ABSTRACT_END_RE = re.compile(
    r"\n\s*(?:keywords?|index terms|(?:1|I)\.?\s+introduction|introduction)\b", re.IGNORECASE
)
_BAD_TITLES = re.compile(r"^(untitled|microsoft word|document\d*|title)\b|\.(docx?|tex|pdf)$", re.I)


class MetaField(BaseModel):
    value: Any = None
    source: str | None = None


class ExtractedMetadata(BaseModel):
    title: MetaField = MetaField()
    authors: MetaField = MetaField()
    doi: MetaField = MetaField()
    arxiv_id: MetaField = MetaField()
    year: MetaField = MetaField()
    abstract: MetaField = MetaField()


def normalize_doi(raw: str) -> str:
    doi = raw.strip().rstrip(".,;:)]}>'\"").lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi)


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _title_from_fonts(page: pymupdf.Page) -> str | None:
    """Largest-font text in the top half of page 1."""
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    spans: list[tuple[float, float, str]] = []
    for block in data["blocks"]:
        for line in block.get("lines", []):
            if abs(line["dir"][1]) > 0.1 or line["bbox"][1] > page.rect.height / 2:
                continue
            for span in line["spans"]:
                text = span["text"].strip()
                if len(text) > 1:
                    spans.append((round(span["size"], 1), line["bbox"][1], text))
    if not spans:
        return None
    top = max(s[0] for s in spans)
    body = sorted(s[0] for s in spans)[len(spans) // 2]
    if top < body * 1.2:
        return None
    parts = [s[2] for s in sorted(spans, key=lambda s: s[1]) if s[0] == top]
    title = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return title if 8 <= len(title) <= 300 else None


def extract_metadata(doc: pymupdf.Document) -> ExtractedMetadata:
    meta = ExtractedMetadata()
    info = doc.metadata or {}
    first_text = "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))

    pdf_title = (info.get("title") or "").strip()
    if len(pdf_title) >= 10 and not _BAD_TITLES.search(pdf_title):
        meta.title = MetaField(value=pdf_title, source="pdf_metadata")
    elif doc.page_count and (font_title := _title_from_fonts(doc[0])):
        meta.title = MetaField(value=font_title, source="page1_largest_font")

    author = (info.get("author") or "").strip()
    if author and len(author) > 2:
        names = [a.strip() for a in re.split(r";|,| and ", author) if a.strip()]
        meta.authors = MetaField(value=names, source="pdf_metadata")

    for source, text in (("pdf_metadata", " ".join(str(v) for v in info.values() if v)),
                         ("first_pages_text", first_text)):
        match = DOI_RE.search(text)
        if match:
            meta.doi = MetaField(value=normalize_doi(match.group(1)), source=source)
            break

    arxiv = ARXIV_RE.search(first_text)
    if arxiv:
        meta.arxiv_id = MetaField(value=arxiv.group(1), source="first_pages_text")
        yymm = arxiv.group(1)[:4]
        meta.year = MetaField(value=2000 + int(yymm[:2]), source="arxiv_id")

    start = ABSTRACT_START_RE.search(first_text)
    if start:
        rest = first_text[start.end():]
        end = ABSTRACT_END_RE.search(rest)
        abstract = re.sub(r"\s+", " ", rest[: end.start() if end else 3000]).strip()
        if len(abstract) >= 100:
            meta.abstract = MetaField(value=abstract[:4000], source="first_pages_heuristic")
    return meta
