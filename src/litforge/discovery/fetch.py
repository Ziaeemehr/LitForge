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
import hashlib
import html
import re
import sqlite3
import urllib.parse
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from litforge.config import Config, Paths
from litforge.discovery.http import HttpClient, HttpError
from litforge.discovery.versions import (
    VersionCandidate,
    format_version_label,
    infer_version,
    link_project_versions,
    other_record_doi,
    printed_dois,
    rank_candidates,
)
from litforge.indexing.fts import current_runs, index_run
from litforge.ingest import ingest_file
from litforge.metadata.extract import extract_metadata, normalize_doi, normalize_title
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
    # One entry per paper: {paper_id, title, outcome (downloaded | failed | no_source), reason, links}.
    items: list[dict[str, object]] = field(default_factory=list)

    def note(self, row: sqlite3.Row, outcome: str, reason: str, links: list[str] | None = None) -> None:
        self.items.append({"paper_id": row["id"], "title": row["title"], "outcome": outcome, "reason": reason,
                           "links": links or []})


def plain_reason(errors: list[str]) -> str:
    """Why no copy could be downloaded, in words a user can act on (the worst-understood host first)."""
    text = " ".join(errors)
    if "HTTP 403" in text or "HTTP 401" in text:
        return "the site blocks automatic downloads"
    if "HTTP 202" in text:
        return "the site asks for a human check first"
    if "PDF link" in text:
        return "the page has no direct PDF link"
    return "; ".join(errors)


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


def openalex_location_records(http: HttpClient, openalex_id: str | None) -> list[dict[str, object]]:
    """OpenAlex OA locations, retaining their explicit version labels."""
    if not openalex_id:
        return []
    try:
        work = http.get_json(f"https://api.openalex.org/works/{openalex_id}?select=locations")
    except Exception:  # noqa: BLE001 - optional extra sources
        return []
    if not isinstance(work, dict):
        return []
    locations = work.get("locations")
    if not isinstance(locations, list):
        return []
    return [loc for loc in locations if isinstance(loc, dict) and loc.get("is_oa") and
            (loc.get("pdf_url") or loc.get("landing_page_url"))]


def openalex_locations(http: HttpClient, openalex_id: str | None) -> tuple[list[str], list[str]]:
    """Open-access copies OpenAlex lists for a work: (repository URLs, publisher URLs), PDFs first."""
    repo_pdf: list[str] = []
    repo_page: list[str] = []
    pub_pdf: list[str] = []
    pub_page: list[str] = []
    for loc in openalex_location_records(http, openalex_id):
        source = loc.get("source")
        is_repo = isinstance(source, dict) and source.get("type") == "repository"
        pdf_url, landing_url = loc.get("pdf_url"), loc.get("landing_page_url")
        if isinstance(pdf_url, str):
            (repo_pdf if is_repo else pub_pdf).append(pdf_url)
        elif isinstance(landing_url, str):
            (repo_page if is_repo else pub_page).append(landing_url)
    return repo_pdf + repo_page, pub_pdf + pub_page


def unpaywall_location_records(http: HttpClient, doi: str | None, email: str | None) -> list[dict[str, object]]:
    """Unpaywall OA locations, retaining their explicit version labels."""
    if not doi or not email:
        return []
    try:
        work = http.get_json(http.url(f"https://api.unpaywall.org/v2/{doi}", {"email": email}))
    except Exception:  # noqa: BLE001 - optional extra source
        return []
    if not isinstance(work, dict):
        return []
    locations = work.get("oa_locations")
    if not isinstance(locations, list):
        return []
    return [loc for loc in locations if isinstance(loc, dict) and
            (loc.get("url_for_pdf") or loc.get("url"))]


def unpaywall_locations(http: HttpClient, doi: str | None, email: str | None) -> tuple[list[str], list[str]]:
    """Open-access copies Unpaywall knows for a DOI: (repository URLs, publisher URLs), PDFs first."""
    repo_pdf: list[str] = []
    repo_page: list[str] = []
    pub_pdf: list[str] = []
    for loc in unpaywall_location_records(http, doi, email):
        is_repo = loc.get("host_type") == "repository"
        pdf_url, landing_url = loc.get("url_for_pdf"), loc.get("url")
        if isinstance(pdf_url, str):
            (repo_pdf if is_repo else pub_pdf).append(pdf_url)
        elif is_repo and isinstance(landing_url, str):
            repo_page.append(landing_url)
    return list(dict.fromkeys(repo_pdf + repo_page)), list(dict.fromkeys(pub_pdf))


def _short(exc: Exception) -> str:
    return f"HTTP {exc.status}" if isinstance(exc, HttpError) else str(exc)[:300]


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
    return _download_pdf(http, url)[0]


def _download_pdf(http: HttpClient, url: str) -> tuple[bytes, str]:
    body, final_url = http.get_bytes_with_url(url, PDF_ACCEPT)
    if not _is_pdf(body):
        link = citation_pdf_url(body, final_url)
        if link is None:
            raise ValueError("not a PDF and no PDF link on the page")
        body, final_url = http.get_bytes_with_url(link, PDF_ACCEPT)
        if not _is_pdf(body):
            raise ValueError("the page's PDF link did not return a PDF")
    if len(body) > MAX_PDF_BYTES:
        raise ValueError(f"PDF larger than {MAX_PDF_BYTES // 2**20} MB")
    return body, final_url


def _title_matches(title: str | None, extracted: str | None, first_page: str) -> bool:
    """Compare a record title with metadata or the title lines at the top of page one."""
    return _title_score(title, extracted, first_page) >= 0.92


def _title_score(title: str | None, extracted: str | None, first_page: str) -> float:
    """Best similarity between a record title and the PDF's metadata title or leading title lines."""
    if not title:
        return 0.0
    wanted = normalize_title(title)
    lines = [line.strip() for line in first_page.splitlines() if line.strip()][:8]
    titles = [extracted or ""]
    # Font/metadata extraction handles journal headers. Without those signals, only the leading
    # title lines count: an early reference or introduction must never supply another paper's title.
    for j, line in enumerate(lines[:4]):
        if re.match(r"(?i)^(?:abstract|(?:\d+\.?\s+)?introduction|references)\b", line):
            break
        titles.append(" ".join(lines[:j + 1]))
    return max((SequenceMatcher(None, wanted, normalize_title(t)).ratio() for t in titles if t), default=0.0)


def _same_work(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """Both records are confirmed versions of one work."""
    return conn.execute(
        """SELECT 1 FROM paper_versions x JOIN paper_versions y ON x.group_id=y.group_id
           WHERE x.paper_id=? AND y.paper_id=? AND x.confirmed=1 AND y.confirmed=1
             AND x.rejected=0 AND y.rejected=0""", (a, b)).fetchone() is not None


def _pdf_identity(conn: sqlite3.Connection, row: sqlite3.Row, body: bytes
                  ) -> tuple[str, tuple[str, str] | None]:
    """Validate identity before accepting a candidate; a citation alone cannot identify its PDF."""
    import pymupdf

    try:
        with pymupdf.open(stream=body, filetype="pdf") as pdf:
            meta = extract_metadata(pdf)
            first_page = pdf[0].get_text() if pdf.page_count else ""
            metadata_doi = (normalize_doi(str(meta.doi.value))
                            if meta.doi.value and meta.doi.source == "pdf_metadata" else None)
            dois = list(dict.fromkeys(([metadata_doi] if metadata_doi else []) + printed_dois(pdf)))
    except Exception:  # noqa: BLE001 - damaged PDFs still go through ingest's quarantine
        return "", None
    own_doi = normalize_doi(str(row["doi"])) if row["doi"] else None
    if own_doi is None and conn.execute(
        "SELECT 1 FROM artifacts WHERE sha256=? AND status='ok'", (hashlib.sha256(body).hexdigest(),)
    ).fetchone():
        # Ingest records byte-identical copies as duplicate candidates, without moving their PDF.
        return first_page, None
    if not dois or own_doi in dois or (own_doi is None and not any(
        other_record_doi(conn, str(row["id"]), None, [doi]) for doi in dois
    )):
        return first_page, None
    arxiv = str(row["arxiv_id"] or "").strip()
    if arxiv and re.search(r"(?i)arxiv:\s*" + re.escape(re.sub(r"v\d+$", "", arxiv)), first_page):
        return first_page, None  # the PDF prints the selected record's own arXiv id
    own_score = _title_score(row["title"], meta.title.value, first_page)
    own_title = own_score >= 0.92
    for doi in dois:
        other = other_record_doi(conn, str(row["id"]), own_doi, [doi])
        if other is None:
            continue
        other_title = conn.execute("SELECT title FROM papers WHERE id=?", (other[0],)).fetchone()[0]
        # A self-identifying preprint stamp followed by its standalone DOI is also positive evidence.
        stamp = re.search(r"(?im)^(?:bio|med|chem|psy)Rxiv preprint[^\n]*\n\s*"
                          r"(?:DOI:\s*)?(?:https?://doi\.org/)?" + re.escape(doi) + r"\s*$", first_page)
        # Versions of one work share near-identical titles: the PDF belongs to whichever record it
        # names more exactly (eLife "..., a flexible ..." vs its bioRxiv preprint "...: A flexible ...").
        other_score = _title_score(other_title, meta.title.value, first_page)
        closer_version = other_score >= 0.92 and other_score > own_score + 0.02 and _same_work(
            conn, str(row["id"]), str(other[0]))  # never for a merely cited paper with a similar title
        if metadata_doi == doi or stamp or (not own_title and other_score >= 0.92) or closer_version:
            return first_page, other
    if own_title:
        return first_page, None
    outside = not any(other_record_doi(conn, str(row["id"]), own_doi, [doi]) for doi in dois)
    reason = "which is not in the library" if outside else "without matching title or metadata evidence"
    raise ValueError(f"found a different work (PDF DOI {dois[0]}), {reason}; "
                     "selected record was left unchanged")


def _attach_elsewhere(conn: sqlite3.Connection, paths: Paths, config: Config, log: EventLog, tmp_dir: Path,
                      row: sqlite3.Row, identity: tuple[str, str], candidate: VersionCandidate, body: bytes,
                      first_page: str, report: FetchReport, new_artifacts: set[str]) -> str:
    """Give a PDF that positively identifies another library record to that record (library only: the
    record is not added to this project). Returns a note for the selected paper's report line."""
    known_id, pdf_doi = identity
    title = (conn.execute("SELECT title FROM papers WHERE id=?", (known_id,)).fetchone() or [None])[0]
    name = f"'{(title or known_id)[:60]}' (DOI {pdf_doi})"
    if conn.execute("SELECT 1 FROM artifacts WHERE paper_id=? AND status='ok' LIMIT 1", (known_id,)).fetchone():
        return f"a PDF of {name} was found; that record already has one"
    tmp = tmp_dir / f"{row['id']}.pdf"
    tmp.write_bytes(body)
    try:
        result = ingest_file(conn, paths, tmp, None, log=log, paper_id=known_id)
    finally:
        tmp.unlink(missing_ok=True)
    # Only a PDF this fetch attached gets its labels: never overwrite an existing artifact's.
    if result.status != "new" or not result.artifact_id or result.paper_id != known_id:
        return f"a PDF of {name} was found; it was already in the library"
    version, version_source = infer_version(candidate.url, candidate.version, candidate.version_source, first_page)
    version_label = format_version_label(version, candidate.url)
    if version == "submitted" and candidate.declared_version == "accepted":
        version_label = f"{version_label} (author-updated)" if version_label else version_label
    conn.execute("UPDATE artifacts SET version=?,version_source=?,source_url=?,version_label=? WHERE id=?",
                 (version, version_source, candidate.url, version_label, result.artifact_id))
    artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (result.artifact_id,)).fetchone()
    moved = parse_artifact(conn, paths, config, artifact, log=log)
    new_artifacts.add(result.artifact_id)
    report.downloaded += 1
    report.parsed += moved.status != "failed"
    return f"a PDF of {name} was found and attached to that library record"


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
    mailto: str | None = None,
) -> FetchReport:
    """Download open-access PDFs of a project's papers that have none: arXiv, the copies OpenAlex lists
    and, when the user gave an e-mail (`mailto`), the copies Unpaywall lists."""
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
        current_pdf = conn.execute(
            "SELECT 1 FROM artifacts WHERE paper_id=? AND status='ok' LIMIT 1", (row["id"],)
        ).fetchone()
        if current_pdf:
            report.note(row, "skipped", "already has a PDF")
            continue
        oa_locations = openalex_location_records(api_http, row["openalex_id"])
        up_locations = unpaywall_location_records(api_http, row["doi"], mailto)
        candidates = rank_candidates(row["arxiv_id"], row["oa_url"], oa_locations, up_locations)
        if not candidates:
            report.no_source += 1
            report.note(row, "no_source", "no free copy is known")
            continue
        body, errors = None, []
        candidate: VersionCandidate | None = None
        first_page_text = ""
        elsewhere: list[str] = []  # PDFs of other known records found on the way
        for item in candidates:
            try:
                downloaded, final_url = _download_pdf(http, item.url)
                text, identity = _pdf_identity(conn, row, downloaded)
                resolved = dataclasses.replace(item, url=final_url)
            except Exception as exc:  # noqa: BLE001 - one bad host must not stop the batch
                errors.append(f"{urllib.parse.urlsplit(item.url).netloc}: {_short(exc)}")
                continue
            if identity is not None:
                elsewhere.append(_attach_elsewhere(conn, paths, config, log, tmp_dir, row, identity, resolved,
                                                   downloaded, text, report, new_artifacts))
                errors.append(f"{urllib.parse.urlsplit(item.url).netloc}: the PDF is another work "
                              f"(DOI {identity[1]})")
                continue
            body, candidate, first_page_text = downloaded, resolved, text
            break
        if body is None:
            link = f"https://doi.org/{row['doi']}" if row["doi"] else candidates[0].url
            report.failed.append(f"{label}: {'; '.join(errors)} (open in a browser: {link})")
            report.note(row, "failed", "; ".join([*elsewhere, plain_reason(errors)]), [c.url for c in candidates])
            log.emit("fetch.failed", paper_id=row["id"], errors=errors)
            continue
        assert candidate is not None
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
            report.note(row, "failed", "the PDF found belongs to another record (a possible duplicate)")
            continue
        if res.status != "new" or res.artifact_id is None:
            report.failed.append(f"{label}: {res.message or res.status}")
            report.note(row, "failed", res.message or res.status)
            continue
        version, version_source = infer_version(candidate.url, candidate.version, candidate.version_source,
                                                first_page_text)
        label_text = "preprint downloaded" if version == "submitted" else f"{version} version downloaded"
        if version == "submitted":
            label_text += "; the published version is not openly available"
        version_label = format_version_label(version, candidate.url)
        if version == "submitted" and candidate.declared_version == "accepted":
            version_label = f"{version_label} (author-updated)" if version_label else version_label
        conn.execute("UPDATE artifacts SET version=?,version_source=?,source_url=?,version_label=? WHERE id=?",
                     (version, version_source, candidate.url, version_label, res.artifact_id))
        report.downloaded += 1
        log.emit("fetch.downloaded", paper_id=row["id"], url=candidate.url, bytes=len(body), version=version)
        artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (res.artifact_id,)).fetchone()
        parsed = parse_artifact(conn, paths, config, artifact, log=log)
        new_artifacts.add(res.artifact_id)
        if parsed.status != "failed":
            report.parsed += 1
            report.note(row, "downloaded",
                        f"{label_text}; full text extracted from {urllib.parse.urlsplit(candidate.url).netloc}")
            report.items[-1]["version"] = version
            report.items[-1]["version_source"] = version_source
        else:
            report.failed.append(f"{label}: downloaded but text extraction failed")
            report.note(row, "downloaded", f"{label_text}, but its text could not be extracted")
            report.items[-1]["version"] = version
            report.items[-1]["version_source"] = version_source
    if new_artifacts:
        progress("indexing the new text")
        for run in current_runs(conn, project_id):
            if run["artifact_id"] in new_artifacts:
                index_run(conn, paths, config, run)
        link_project_versions(conn, api_http, mailto, project_id)
    return report
