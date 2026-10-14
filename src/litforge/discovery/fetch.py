"""Download legal open-access PDFs for a project's papers, then parse and index them like `add`.

Sources, in order: arXiv (from the paper's arXiv id), repository copies OpenAlex knows of (university
repositories, Zenodo...), the open-access URL found by discovery, then other publisher copies. When a
URL is a landing page, the PDF link is taken from its `citation_pdf_url` meta tag (the standard
publishers expose for Google Scholar). Nothing is downloaded for papers that already have a PDF.
Publishers behind bot protection (HTTP 403) and PMC's download challenge are not worked around: those
papers are reported with a link to open in a browser.
"""

from __future__ import annotations

import dataclasses
import html
import re
import sqlite3
import urllib.parse
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field

from litforge.config import Config, Paths
from litforge.discovery.http import HttpClient, HttpError
from litforge.indexing.fts import current_runs, index_run
from litforge.ingest import ingest_file
from litforge.observability import NULL_LOG, EventLog
from litforge.parsing.run import parse_artifact
from litforge.projects import MARK_SCOPES
from litforge.storage.db import now, transaction

MAX_PDF_BYTES = 100 * 1024 * 1024
PDF_ACCEPT = {"Accept": "application/pdf,text/html;q=0.8,*/*;q=0.5"}
_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"""(\w[\w-]*)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")


@dataclass
class FetchReport:
    candidates: int = 0  # selected papers without a PDF
    downloaded: int = 0
    parsed: int = 0
    no_source: int = 0  # no arXiv id and no open-access URL
    failed: list[str] = field(default_factory=list)


def candidate_urls(arxiv_id: str | None, oa_url: str | None,
                   repository: Sequence[str] = (), publisher: Sequence[str] = ()) -> list[str]:
    urls = [f"https://arxiv.org/pdf/{arxiv_id}"] if arxiv_id else []
    for url in [*repository, oa_url, *publisher]:
        if not url or not url.startswith(("http://", "https://")) or url in urls:
            continue
        if arxiv_id and "arxiv.org" in url:
            continue
        urls.append(url)
    return urls


def openalex_locations(http: HttpClient, openalex_id: str | None) -> tuple[list[str], list[str]]:
    """Open-access copies OpenAlex lists for a work: (repository URLs, publisher URLs), PDFs first.
    Single-work lookups are free; any failure just means fewer candidates."""
    if not openalex_id:
        return [], []
    try:
        work = http.get_json(f"https://api.openalex.org/works/{openalex_id}?select=locations")
    except Exception:  # noqa: BLE001 - optional extra sources
        return [], []
    repo_pdf: list[str] = []
    repo_page: list[str] = []
    pub_pdf: list[str] = []
    pub_page: list[str] = []
    for loc in work.get("locations") or []:
        if not loc.get("is_oa"):
            continue
        is_repo = (loc.get("source") or {}).get("type") == "repository"
        if loc.get("pdf_url"):
            (repo_pdf if is_repo else pub_pdf).append(loc["pdf_url"])
        elif loc.get("landing_page_url"):
            (repo_page if is_repo else pub_page).append(loc["landing_page_url"])
    return repo_pdf + repo_page, pub_pdf + pub_page


def _short(exc: Exception) -> str:
    return f"HTTP {exc.status}" if isinstance(exc, HttpError) else str(exc)[:120]


def citation_pdf_url(page: bytes, base: str) -> str | None:
    text = page[:500_000].decode("utf-8", errors="ignore")
    for tag in _META.findall(text):
        attrs = {k.lower(): v.strip("\"'") for k, v in _ATTR.findall(tag)}
        if attrs.get("name", "").lower() == "citation_pdf_url" and attrs.get("content"):
            return urllib.parse.urljoin(base, html.unescape(attrs["content"]))
    return None


def _is_pdf(body: bytes) -> bool:
    return b"%PDF-" in body[:1024]


def download_pdf(http: HttpClient, url: str) -> bytes:
    body = http.get_bytes(url, PDF_ACCEPT)
    if not _is_pdf(body):
        link = citation_pdf_url(body, url)
        if link is None:
            raise ValueError("not a PDF and no PDF link on the page")
        body = http.get_bytes(link, PDF_ACCEPT)
        if not _is_pdf(body):
            raise ValueError("the page's PDF link did not return a PDF")
    if len(body) > MAX_PDF_BYTES:
        raise ValueError(f"PDF larger than {MAX_PDF_BYTES // 2**20} MB")
    return body


def fetch_open_access(
    conn: sqlite3.Connection,
    paths: Paths,
    config: Config,
    http: HttpClient,
    project_id: str,
    only: str = "relevant",
    paper_ids: Collection[str] | None = None,
    log: EventLog = NULL_LOG,
    progress: Callable[[str], None] = lambda _: None,
) -> FetchReport:
    if only not in MARK_SCOPES:
        raise ValueError(f"only must be one of {sorted(MARK_SCOPES)}")
    rows = conn.execute(
        f"""SELECT p.id, p.title, p.arxiv_id, p.oa_url, p.openalex_id, p.doi FROM project_papers pp
            JOIN papers p ON p.id = pp.paper_id
            LEFT JOIN paper_marks m ON m.paper_id = p.id AND m.project_id = pp.project_id
            WHERE pp.project_id = ?{MARK_SCOPES[only]}
              AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.paper_id = p.id AND a.status = 'ok')
              AND NOT EXISTS (SELECT 1 FROM duplicate_candidates d
                              WHERE d.paper_id_b = p.id AND d.reason = 'same open-access PDF')
            ORDER BY p.year DESC, p.title""",
        (project_id,),
    ).fetchall()
    if paper_ids is not None:
        wanted = set(paper_ids)
        rows = [r for r in rows if r["id"] in wanted]
    report = FetchReport(candidates=len(rows))
    api_http = http
    # PDFs are binary and large: never through the JSON response cache.
    http = dataclasses.replace(http, cache_dir=None, timeout=max(http.timeout, 60.0))
    tmp_dir = paths.root / "cache" / "downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    new_artifacts: set[str] = set()
    for i, row in enumerate(rows, 1):
        label = (row["title"] or row["id"])[:80]
        progress(f"[{i}/{len(rows)}] {label}")
        urls = candidate_urls(row["arxiv_id"], row["oa_url"], *openalex_locations(api_http, row["openalex_id"]))
        if not urls:
            report.no_source += 1
            continue
        body, errors = None, []
        for url in urls:
            try:
                body = download_pdf(http, url)
                break
            except Exception as exc:  # noqa: BLE001 - one bad host must not stop the batch
                errors.append(f"{urllib.parse.urlsplit(url).netloc}: {_short(exc)}")
        if body is None:
            link = f"https://doi.org/{row['doi']}" if row["doi"] else urls[0]
            report.failed.append(f"{label}: {'; '.join(errors)} (open in a browser: {link})")
            log.emit("fetch.failed", paper_id=row["id"], errors=errors)
            continue
        tmp = tmp_dir / f"{row['id']}.pdf"
        tmp.write_bytes(body)
        try:
            # No project here: the paper is already a member, and a duplicate must not pull its twin in.
            res = ingest_file(conn, paths, tmp, None, log=log, paper_id=row["id"])
        finally:
            tmp.unlink(missing_ok=True)
        if res.status == "duplicate" and res.paper_id and res.paper_id != row["id"]:
            # The same PDF already belongs to another record: most likely the same work twice.
            with transaction(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO duplicate_candidates(paper_id_a, paper_id_b, reason, created_at)"
                    " VALUES (?,?,?,?)", (res.paper_id, row["id"], "same open-access PDF", now()))
            report.failed.append(f"{label}: same PDF as another library record (listed as a possible duplicate)")
            continue
        if res.status != "new" or res.artifact_id is None:
            report.failed.append(f"{label}: {res.message or res.status}")
            continue
        report.downloaded += 1
        log.emit("fetch.downloaded", paper_id=row["id"], url=url, bytes=len(body))
        artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (res.artifact_id,)).fetchone()
        parsed = parse_artifact(conn, paths, config, artifact, log=log)
        new_artifacts.add(res.artifact_id)
        if parsed.status != "failed":
            report.parsed += 1
        else:
            report.failed.append(f"{label}: downloaded but text extraction failed")
    if new_artifacts:
        progress("indexing the new text")
        for run in current_runs(conn, project_id):
            if run["artifact_id"] in new_artifacts:
                index_run(conn, paths, config, run)
    return report


