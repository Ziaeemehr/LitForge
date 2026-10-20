"""Papers from links: an article's web page, a doi.org / arXiv / OpenAlex link, or a direct link to a PDF.

A page is read only for the tags publishers put there for indexers (citation_doi, dc.identifier,
citation_arxiv_id); DOIs elsewhere on the page belong to its references and are never used."""

from __future__ import annotations

import hashlib
import html
import re
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

from litforge.config import Paths
from litforge.discovery.records import normalize_arxiv_id
from litforge.discovery.run import SOURCE_ERRORS, DiscoveryReport, Sources, _Run

_DOI = re.compile(r"10\.\d{4,9}/[^\s?#\"'<>]+")
_ARXIV_PATH = re.compile(r"^/(?:abs|pdf|html)/([^?#]+?)(?:\.pdf)?/?$")
_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""(name|property|content)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_DOI_TAGS = {"citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi", "bepress_citation_doi", "eprints.doi"}
MAX_PAGE_BYTES = 30 * 1024 * 1024


def identifier_from_url(url: str) -> str | None:
    """A DOI, arXiv id or OpenAlex id written in the link itself, or None when the page must be read."""
    parts = urlsplit(url.strip())
    host, path = parts.netloc.lower().removeprefix("www."), unquote(parts.path)
    if host in ("doi.org", "dx.doi.org"):
        return path.lstrip("/") or None
    if host in ("arxiv.org", "export.arxiv.org") and (m := _ARXIV_PATH.match(path)):
        return normalize_arxiv_id(m.group(1))
    if host == "openalex.org" and re.fullmatch(r"/W\d+", path):
        return path[1:]
    if m := _DOI.search(path + ("?" + unquote(parts.query) if parts.query else "")):
        return _clean_doi(m.group(0))
    return None


def identifier_from_html(page: str) -> str | None:
    """The DOI (or arXiv id) a publisher's page declares for itself in its meta tags."""
    arxiv = None
    for tag in _META.findall(page):
        attrs = {m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
                 for m in _ATTR.finditer(tag)}
        name = (attrs.get("name") or attrs.get("property") or "").lower()
        content = attrs.get("content", "").strip()
        if name in _DOI_TAGS and (m := _DOI.search(content)):
            return _clean_doi(m.group(0))
        if name == "citation_arxiv_id" and content:
            arxiv = normalize_arxiv_id(content)
    return arxiv


def _clean_doi(doi: str) -> str:
    return doi.rstrip(".,;)]")


def run_links(conn: sqlite3.Connection, sources: Sources, paths: Paths, project_id: str, urls: Sequence[str],
              progress: Callable[[str], None] = lambda _: None) -> tuple[DiscoveryReport, list[str]]:
    """Add each link's paper to the project (as one run). Direct PDF links are saved in uploads/ and returned
    for reading like the user's own PDFs."""
    run = _Run(conn, project_id, "links", {"links": list(urls)})
    pdfs: list[str] = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        if not re.match(r"https?://", url):
            run.error(url[:80], ValueError("not a link (it must start with http:// or https://)"))
            continue
        progress(f"reading {url}")
        try:
            ident = identifier_from_url(url)
            if ident is None:
                body, _final = sources.http.get_bytes_with_url(url, {"Accept": "text/html,application/pdf;q=0.9"})
                if body[:5] == b"%PDF-":
                    pdfs.append(_save_pdf(paths, url, body))
                    continue
                ident = identifier_from_html(body[:MAX_PAGE_BYTES].decode("utf-8", "replace"))
            if ident is None:
                run.error(url, ValueError("no DOI or arXiv id found on this page; paste the DOI instead"))
                continue
            record = sources.openalex.get(ident)
        except SOURCE_ERRORS as exc:
            run.error(url, exc)
            continue
        if record is None:
            run.error(url, ValueError(f"{ident} was not found in OpenAlex"))
            continue
        run.add([record], "link", "link", query=url)
    report = run.finish(sources.http, 0)
    if pdfs and report.status == "failed" and not report.hits:
        report.status = "partial" if report.errors else "done"
    return report, pdfs


def _save_pdf(paths: Paths, url: str, body: bytes) -> str:
    uploads = paths.root / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\-]+", "_", Path(urlsplit(url).path).name)[:80] or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    target = uploads / f"{hashlib.sha256(body).hexdigest()[:12]}-{name}"
    target.write_bytes(body)
    return str(target)
