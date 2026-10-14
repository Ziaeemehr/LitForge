"""Source-independent bibliographic record and identifier normalization."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from litforge.metadata.extract import normalize_doi

# Sources whose DOIs come from the registry itself (not author-supplied).
REGISTRY_SOURCES = {"openalex", "crossref"}

_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?$")
_ARXIV_OLD = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$", re.IGNORECASE)
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)


def normalize_arxiv_id(raw: str | None) -> str | None:
    """'arXiv:1905.07488v2', 'https://arxiv.org/abs/1905.07488' -> '1905.07488' (no version)."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^(?:https?://)?(?:www\.)?(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.I).removesuffix(".pdf")
    for pattern in (_ARXIV_NEW, _ARXIV_OLD):
        match = pattern.search(text)
        if match and match.end() == len(text):
            return match.group(1)
    return None


class WorkRecord(BaseModel):
    source: str
    source_id: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    abstract: str | None = None
    cited_by_count: int | None = None
    oa_url: str | None = None
    work_type: str | None = None
    references: list[str] = Field(default_factory=list)  # OpenAlex ids this work cites
    related: list[str] = Field(default_factory=list)  # OpenAlex "related works" ids
    note: str | None = None  # free-text note from an imported list (e.g. Elicit's "Relevance")
    fwci: float | None = None
    citation_percentile: float | None = None
    recent_citations: int | None = None
    is_retracted: bool | None = None
    venue_id: str | None = None  # OpenAlex venue id

    def normalized(self) -> WorkRecord:
        rec = self.model_copy()
        if rec.doi:
            rec.doi = normalize_doi(rec.doi)
            arxiv_doi = _ARXIV_DOI.match(rec.doi)
            if arxiv_doi and not rec.arxiv_id:
                rec.arxiv_id = arxiv_doi.group(1)
        rec.arxiv_id = normalize_arxiv_id(rec.arxiv_id) if rec.arxiv_id else None
        if rec.title:
            rec.title = re.sub(r"\s+", " ", rec.title).strip() or None
        if rec.abstract:
            rec.abstract = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", rec.abstract)).strip() or None
        if rec.year is not None and not 1600 <= rec.year <= 2100:
            rec.year = None
        return rec

    @property
    def doi_is_verified(self) -> bool:
        return self.source in REGISTRY_SOURCES and self.doi is not None
