"""Adapters for OpenAlex, Crossref and arXiv. Each returns normalized WorkRecords.

Notes on the live APIs (checked 2026-10):
- OpenAlex is metered: without a key there is a small free daily USD budget; a search costs
  $0.001, a filter query $0.0001, a single-work lookup is free. `title_and_abstract.search` is
  used instead of `search` (which matches full text and is far too broad).
- arXiv asks for at most one request every 3 seconds (enforced in HttpClient).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Protocol

from litforge.discovery.http import HttpClient, HttpError
from litforge.discovery.records import WorkRecord, normalize_arxiv_id

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
ARXIV = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

_OPENALEX_SELECT = ",".join([
    "id", "doi", "title", "publication_year", "type", "cited_by_count", "authorships",
    "primary_location", "best_oa_location", "open_access", "abstract_inverted_index",
])


# Registry records that are not research papers.
EXCLUDED_TYPES = {
    "peer-review", "paratext", "erratum", "retraction", "editorial", "letter", "grant", "component",
    "dataset", "journal-issue", "journal-volume", "journal", "proceedings", "book-series", "supplementary-materials",
}


def _papers_only(records: list[WorkRecord]) -> list[WorkRecord]:
    return [r for r in records if r.work_type not in EXCLUDED_TYPES]


class SearchSource(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[WorkRecord]: ...


def _clean_query(query: str) -> str:
    """Filter values in OpenAlex use ',' and ':' as syntax; keep words, quotes and hyphens."""
    return re.sub(r"[,:|()]+", " ", query).strip()


def _abstract_from_inverted(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    words: dict[int, str] = {}
    for word, positions in index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words)) or None


class OpenAlex:
    name = "openalex"

    def __init__(self, http: HttpClient, api_key: str | None = None, mailto: str | None = None) -> None:
        self.http = http
        self.api_key = api_key
        self.mailto = mailto

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = {"select": _OPENALEX_SELECT, "api_key": self.api_key, "mailto": self.mailto, **(params or {})}
        return self.http.get_json(self.http.url(f"{OPENALEX}{path}", query))

    def _list(self, params: dict[str, Any], limit: int) -> list[WorkRecord]:
        records: list[WorkRecord] = []
        page = 1
        while len(records) < limit:
            per_page = min(200, limit - len(records))
            data = self._get("/works", {**params, "per-page": per_page, "page": page})
            batch = [self.parse(w) for w in data.get("results", [])]
            records += _papers_only(batch)
            if len(batch) < per_page:
                break
            page += 1
        return records[:limit]

    def search(self, query: str, limit: int) -> list[WorkRecord]:
        return self._list({"filter": f"title_and_abstract.search:{_clean_query(query)}"}, limit)

    def get(self, identifier: str) -> WorkRecord | None:
        """identifier: 'W123', a DOI, or an arXiv id (resolved through arXiv's DataCite DOI)."""
        identifier = identifier.strip().removeprefix("https://doi.org/")
        if re.fullmatch(r"W\d+", identifier):
            path = f"/works/{identifier}"
        elif identifier.startswith("10."):  # DOIs first: some DOIs end in arXiv-like digits
            path = f"/works/doi:{identifier}"
        elif arxiv := normalize_arxiv_id(identifier):
            path = f"/works/doi:10.48550/arXiv.{arxiv}"
        else:
            path = f"/works/doi:{identifier.removeprefix('https://doi.org/')}"
        try:
            return self.parse(self._get(path))
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def references(self, openalex_id: str, limit: int) -> list[WorkRecord]:
        """Works cited by `openalex_id`."""
        return self._list({"filter": f"cited_by:{openalex_id}"}, limit)

    def citing(self, openalex_id: str, limit: int) -> list[WorkRecord]:
        """Works citing `openalex_id`, most cited first."""
        return self._list({"filter": f"cites:{openalex_id}", "sort": "cited_by_count:desc"}, limit)

    def title_lookup(self, title: str, limit: int = 5) -> list[WorkRecord]:
        return self._list({"filter": f"title.search:{_clean_query(title)}"}, limit)

    @staticmethod
    def parse(work: dict[str, Any]) -> WorkRecord:
        primary = work.get("primary_location") or {}
        best_oa = work.get("best_oa_location") or {}
        landing_urls = [loc.get("landing_page_url") or "" for loc in (primary, best_oa)]
        arxiv = next((normalize_arxiv_id(u) for u in landing_urls if "arxiv.org" in u), None)
        openalex_id = (work.get("id") or "").rsplit("/", 1)[-1]
        return WorkRecord(
            source="openalex",
            source_id=openalex_id,
            openalex_id=openalex_id or None,
            title=work.get("title"),
            authors=[a["author"]["display_name"] for a in work.get("authorships") or []
                     if a.get("author", {}).get("display_name")],
            year=work.get("publication_year"),
            venue=((primary.get("source") or {}).get("display_name")),
            doi=work.get("doi"),
            arxiv_id=arxiv,
            abstract=_abstract_from_inverted(work.get("abstract_inverted_index")),
            cited_by_count=work.get("cited_by_count"),
            oa_url=best_oa.get("pdf_url") or best_oa.get("landing_page_url")
            or (work.get("open_access") or {}).get("oa_url"),
            work_type=work.get("type"),
        ).normalized()


class Crossref:
    name = "crossref"
    _SELECT = "DOI,title,author,issued,container-title,abstract,is-referenced-by-count,type"

    def __init__(self, http: HttpClient, mailto: str | None = None) -> None:
        self.http = http
        self.mailto = mailto

    def search(self, query: str, limit: int) -> list[WorkRecord]:
        url = self.http.url(f"{CROSSREF}/works", {
            "query.bibliographic": query, "rows": min(limit, 100), "select": self._SELECT, "mailto": self.mailto,
        })
        items = self.http.get_json(url).get("message", {}).get("items", [])
        return _papers_only([self.parse(i) for i in items])

    def get(self, doi: str) -> WorkRecord | None:
        try:
            data = self.http.get_json(self.http.url(f"{CROSSREF}/works/{doi}", {"mailto": self.mailto}))
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return self.parse(data.get("message", {}))

    @staticmethod
    def parse(item: dict[str, Any]) -> WorkRecord:
        parts = (item.get("issued") or {}).get("date-parts") or [[None]]
        year = parts[0][0] if parts and parts[0] else None
        authors = [" ".join(p for p in (a.get("given"), a.get("family")) if p) or a.get("name", "")
                   for a in item.get("author") or []]
        return WorkRecord(
            source="crossref",
            source_id=item.get("DOI", ""),
            doi=item.get("DOI"),
            title=(item.get("title") or [None])[0],
            authors=[a for a in authors if a],
            year=year if isinstance(year, int) else None,
            venue=(item.get("container-title") or [None])[0],
            abstract=item.get("abstract"),  # JATS XML; tags stripped by normalized()
            cited_by_count=item.get("is-referenced-by-count"),
            work_type=item.get("type"),
        ).normalized()


class Arxiv:
    name = "arxiv"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @staticmethod
    def build_query(query: str) -> str:
        phrases = re.findall(r'"([^"]+)"', query)
        words = re.findall(r"[\w\-]+", re.sub(r'"[^"]+"', " ", query))
        parts = [f'all:"{p}"' for p in phrases] + [f"all:{w}" for w in words if len(w) > 2]
        return " AND ".join(parts) or "all:*"

    def search(self, query: str, limit: int) -> list[WorkRecord]:
        url = self.http.url(ARXIV, {"search_query": self.build_query(query), "max_results": min(limit, 100),
                                    "sortBy": "relevance"})
        return self.parse_feed(self.http.get_bytes(url, {"Accept": "application/atom+xml"}))

    def get(self, arxiv_id: str) -> WorkRecord | None:
        url = self.http.url(ARXIV, {"id_list": arxiv_id, "max_results": 1})
        records = self.parse_feed(self.http.get_bytes(url, {"Accept": "application/atom+xml"}))
        return records[0] if records else None

    @staticmethod
    def parse_feed(body: bytes) -> list[WorkRecord]:
        root = ET.fromstring(body)
        out = []
        for entry in root.findall("a:entry", ATOM):
            entry_id = entry.findtext("a:id", default="", namespaces=ATOM)
            arxiv_id = normalize_arxiv_id(entry_id)
            title = entry.findtext("a:title", default="", namespaces=ATOM)
            if not arxiv_id or title.strip().lower() == "error":
                continue
            published = entry.findtext("a:published", default="", namespaces=ATOM)
            pdf = next((ln.get("href") for ln in entry.findall("a:link", ATOM) if ln.get("title") == "pdf"), None)
            out.append(WorkRecord(
                source="arxiv",
                source_id=arxiv_id,
                arxiv_id=arxiv_id,
                title=title,
                authors=[a.findtext("a:name", default="", namespaces=ATOM) for a in entry.findall("a:author", ATOM)],
                year=int(published[:4]) if published[:4].isdigit() else None,
                venue="arXiv",
                doi=entry.findtext("arxiv:doi", default=None, namespaces=ATOM),
                abstract=entry.findtext("a:summary", default=None, namespaces=ATOM),
                oa_url=pdf,
                work_type="preprint",
            ).normalized())
        return out
