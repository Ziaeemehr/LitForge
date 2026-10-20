"""PDF ingestion: hash, deduplicate, store the original, extract metadata, record membership."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import pymupdf
from pydantic import BaseModel

from litforge.config import Paths
from litforge.discovery.merge import apply_record, ensure_norm_titles, find_match
from litforge.discovery.records import WorkRecord
from litforge.discovery.versions import infer_version
from litforge.metadata.extract import ExtractedMetadata, extract_metadata, normalize_title
from litforge.observability import NULL_LOG, EventLog
from litforge.parsing.markitdown_parser import document_title, is_document
from litforge.projects import add_paper
from litforge.storage.cache import relative_to_root, sha256_file, store_document, store_pdf
from litforge.storage.db import dumps, new_id, now, transaction

IngestStatus = Literal["new", "duplicate", "quarantined", "error"]


class IngestResult(BaseModel):
    path: str
    status: IngestStatus
    sha256: str | None = None
    paper_id: str | None = None
    artifact_id: str | None = None
    message: str | None = None
    possible_duplicates: list[str] = []


def iter_pdfs(sources: Iterable[Path], documents: bool = False) -> list[Path]:
    """PDFs in the given files and folders; with `documents`, also Word, PowerPoint, Excel, HTML, EPUB,
    Markdown and text files found in the folders."""
    found: list[Path] = []
    for src in sources:
        if src.is_dir():
            found += [p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")
                      and (p.suffix.lower() == ".pdf" or (documents and is_document(p)))]
        elif src.is_file():
            found.append(src)
    return sorted({p.resolve() for p in found})


def _validate(path: Path) -> tuple[pymupdf.Document | None, str | None]:
    """Open the PDF; return (doc, None) or (None, reason) for quarantine."""
    with path.open("rb") as fh:
        if b"%PDF-" not in fh.read(1024):
            return None, "not a PDF (missing %PDF- header)"
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - any open failure means quarantine
        return None, f"cannot open: {exc}"
    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        return None, "encrypted / password protected"
    if doc.page_count == 0:
        doc.close()
        return None, "PDF has no pages"
    return doc, None


def _add_alias(conn: sqlite3.Connection, artifact_id: str, path: Path) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO artifact_aliases(artifact_id, original_path, filename, first_seen_at)"
        " VALUES (?,?,?,?)",
        (artifact_id, str(path), path.name, now()),
    )


def _near_duplicates(conn: sqlite3.Connection, meta: ExtractedMetadata) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    if meta.doi.value:
        for row in conn.execute("SELECT id FROM papers WHERE doi=?", (meta.doi.value,)):
            hits.append((row["id"], "doi"))
    if meta.title.value and len(norm := normalize_title(meta.title.value)) >= 20:
        for row in conn.execute("SELECT id FROM papers WHERE norm_title=?", (norm,)):
            if (row["id"], "doi") not in hits:
                hits.append((row["id"], "title"))
    return hits


def ingest_file(
    conn: sqlite3.Connection,
    paths: Paths,
    path: Path,
    project_id: str | None,
    dry_run: bool = False,
    log: EventLog = NULL_LOG,
    paper_id: str | None = None,
) -> IngestResult:
    """Add one PDF. With `paper_id` (a download for a known record) the file is attached to that paper
    instead of being matched by the metadata extracted from the PDF."""
    sha = sha256_file(path)
    existing = conn.execute(
        "SELECT id, paper_id, status FROM artifacts WHERE sha256=?", (sha,)
    ).fetchone()
    if existing is not None:
        if not dry_run:
            with transaction(conn):
                _add_alias(conn, existing["id"], path)
                if project_id and existing["paper_id"]:
                    add_paper(conn, project_id, existing["paper_id"])
        msg = "identical file already in library"
        if existing["status"] == "quarantined":
            msg += " (quarantined)"
        return IngestResult(path=str(path), status="duplicate", sha256=sha,
                            paper_id=existing["paper_id"], artifact_id=existing["id"], message=msg)

    doc, reason = _validate(path)
    if dry_run:
        doc and doc.close()
        status: IngestStatus = "quarantined" if reason else "new"
        return IngestResult(path=str(path), status=status, sha256=sha, message=reason)

    stored = store_pdf(paths, path, sha)  # originals are kept even when quarantined
    artifact_id = new_id()
    ts = now()
    if doc is None:
        with transaction(conn):
            conn.execute(
                "INSERT INTO artifacts(id, paper_id, sha256, pdf_path, size_bytes, status, error, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (artifact_id, None, sha, relative_to_root(paths, stored), path.stat().st_size,
                 "quarantined", reason, ts),
            )
            _add_alias(conn, artifact_id, path)
        log.emit("ingest.quarantined", path=str(path), sha256=sha, reason=reason)
        return IngestResult(path=str(path), status="quarantined", sha256=sha,
                            artifact_id=artifact_id, message=reason)

    try:
        meta = extract_metadata(doc)
        page_count = doc.page_count
        first_page_text = doc[0].get_text() if page_count else ""
    finally:
        doc.close()
    version, version_source = infer_version(None, pdf_text=first_page_text)
    pdf_rec = WorkRecord(
        source="pdf", source_id=sha, title=meta.title.value, year=meta.year.value, doi=meta.doi.value,
        arxiv_id=meta.arxiv_id.value, abstract=meta.abstract.value, authors=meta.authors.value or [],
    ).normalized()
    if paper_id is not None:
        match: str | None = paper_id
    else:
        ensure_norm_titles(conn)
        match, _ = find_match(conn, pdf_rec)
    has_pdf = match is not None and conn.execute(
        "SELECT 1 FROM artifacts WHERE paper_id=? AND status='ok'", (match,)
    ).fetchone() is not None
    if match is not None and not has_pdf:
        # A paper found by discovery (or imported from a reference list) now gets its PDF.
        with transaction(conn):
            conn.execute(
                "INSERT INTO artifacts(id, paper_id, sha256, pdf_path, size_bytes, page_count, version,"
                " version_source, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, match, sha, relative_to_root(paths, stored), path.stat().st_size, page_count,
                 version, version_source, "ok", ts),
            )
            _add_alias(conn, artifact_id, path)
            apply_record(conn, match, pdf_rec, ts)
            if project_id:
                add_paper(conn, project_id, match)
        log.emit("ingest.attached", path=str(path), sha256=sha, paper_id=match, pages=page_count)
        return IngestResult(path=str(path), status="new", sha256=sha, paper_id=match, artifact_id=artifact_id,
                            message="attached to an existing library record")

    paper_id = new_id()  # no record to attach to: the PDF becomes a new paper
    dupes = _near_duplicates(conn, meta)
    with transaction(conn):
        conn.execute(
            "INSERT INTO papers(id, title, doi, doi_status, year, authors_json, abstract, metadata_json,"
            " arxiv_id, norm_title, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                paper_id,
                pdf_rec.title,
                pdf_rec.doi,
                "unverified" if pdf_rec.doi else None,
                pdf_rec.year,
                dumps(pdf_rec.authors) if pdf_rec.authors else None,
                pdf_rec.abstract,
                meta.model_dump_json(),
                pdf_rec.arxiv_id,
                normalize_title(pdf_rec.title) if pdf_rec.title else None,
                ts,
                ts,
            ),
        )
        conn.execute(
            "INSERT INTO artifacts(id, paper_id, sha256, pdf_path, size_bytes, page_count, version, version_source,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, paper_id, sha, relative_to_root(paths, stored), path.stat().st_size,
             page_count, version, version_source, "ok", ts),
        )
        _add_alias(conn, artifact_id, path)
        for other_id, why in dupes:
            conn.execute(
                "INSERT OR IGNORE INTO duplicate_candidates(paper_id_a, paper_id_b, reason, created_at)"
                " VALUES (?,?,?,?)",
                (other_id, paper_id, why, ts),
            )
        if project_id:
            add_paper(conn, project_id, paper_id)
    log.emit("ingest.new", path=str(path), sha256=sha, paper_id=paper_id, pages=page_count)
    return IngestResult(
        path=str(path), status="new", sha256=sha, paper_id=paper_id, artifact_id=artifact_id,
        possible_duplicates=[d[0] for d in dupes],
        message=f"possible duplicate of {len(dupes)} paper(s); review" if dupes else None,
    )


def ingest_document(conn: sqlite3.Connection, paths: Paths, path: Path, project_id: str | None,
                    log: EventLog = NULL_LOG) -> IngestResult:
    """Add one document that is not a PDF: stored with its own extension, a new paper titled by its first
    heading (or its file name). Its text is read later by the MarkItDown parser."""
    sha = sha256_file(path)
    existing = conn.execute("SELECT id, paper_id FROM artifacts WHERE sha256=?", (sha,)).fetchone()
    if existing is not None:
        with transaction(conn):
            _add_alias(conn, existing["id"], path)
            if project_id and existing["paper_id"]:
                add_paper(conn, project_id, existing["paper_id"])
        return IngestResult(path=str(path), status="duplicate", sha256=sha, paper_id=existing["paper_id"],
                            artifact_id=existing["id"], message="identical file already in library")
    stored = store_document(paths, path, sha)
    title = document_title(path) or re.sub(r"^[0-9a-f]{12}-", "", path.stem).replace("_", " ")
    paper_id, artifact_id, ts = new_id(), new_id(), now()
    with transaction(conn):
        conn.execute("INSERT INTO papers(id, title, norm_title, created_at, updated_at) VALUES (?,?,?,?,?)",
                     (paper_id, title, normalize_title(title), ts, ts))
        conn.execute(
            "INSERT INTO artifacts(id, paper_id, sha256, pdf_path, size_bytes, page_count, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (artifact_id, paper_id, sha, relative_to_root(paths, stored), path.stat().st_size, 1, "ok", ts))
        _add_alias(conn, artifact_id, path)
        if project_id:
            add_paper(conn, project_id, paper_id)
    log.emit("ingest.document", path=str(path), sha256=sha, paper_id=paper_id)
    return IngestResult(path=str(path), status="new", sha256=sha, paper_id=paper_id, artifact_id=artifact_id)


def ingest_paths(
    conn: sqlite3.Connection,
    paths: Paths,
    sources: Iterable[Path],
    project_id: str | None,
    dry_run: bool = False,
    log: EventLog = NULL_LOG,
) -> list[IngestResult]:
    results = []
    for pdf in iter_pdfs(sources):
        try:
            results.append(ingest_file(conn, paths, pdf, project_id, dry_run, log))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            log.emit("ingest.error", path=str(pdf), error=repr(exc))
            results.append(IngestResult(path=str(pdf), status="error", message=repr(exc)))
    return results
