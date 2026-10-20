"""Collect unused papers from the shared library, preserving cited audit evidence.

A paper must have no project membership and no span referenced by any surviving
analysis or audit record (including JSON payloads, dropped IDs, snapshots and
reviews). Detached runs also count. Repro links owned by the paper are removable
cache rows; links owned by another paper protect their evidence. Migration 0020
permits deleting only spans verified by cleanup; update/replacement remain forbidden.
Paper-owned screening history and claim reviews are collected with their papers;
surviving audit records protect their evidence. Refusals are always reported.

The database changes commit together after rechecking eligibility under a write
lock. Only canonical library/pdfs/<sha>.pdf and library/parsed/<sha> paths are
moved into a private trash directory before commit, then collected afterward.
Rollback restores staged files. Recorded paths, aliases and user exports are
never followed. Failures collecting trash are reported after committed removal.
Byte counts cover files, not SQLite space (which SQLite may reuse).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from litforge.config import Paths
from litforge.parsing.markitdown_parser import DOCUMENT_SUFFIXES


class RemovalPaper(BaseModel):
    id: str
    title: str | None


class RemovalIssue(BaseModel):
    id: str
    reason: str


class FileIssue(BaseModel):
    path: str
    reason: str


class RemovalPreview(BaseModel):
    papers: list[RemovalPaper] = Field(default_factory=list)
    refused: list[RemovalIssue] = Field(default_factory=list)
    paper_count: int = 0
    pdf_count: int = 0
    parsed_folder_count: int = 0
    bytes_freed: int = 0
    file_errors: list[FileIssue] = Field(default_factory=list)


class RemovalReport(RemovalPreview):
    removed: list[str] = Field(default_factory=list)


@dataclass
class _File:
    path: Path
    parsed: bool
    size: int


def _safe_path(paths: Paths, path: Path) -> None:
    """Reject symbolic links at every level below the configured data root."""
    root = paths.root.absolute()
    try:
        parts = path.absolute().relative_to(root).parts
    except ValueError as exc:
        raise OSError('path is outside the data root') from exc
    current = root
    for part in parts:
        if part in ('.', '..'):
            raise OSError('unsafe library path')
        current /= part
        if current.is_symlink():
            raise OSError('refusing a symbolic link in a library path')
    if not path.resolve().is_relative_to(root.resolve()):
        raise OSError('path is outside the data root')


def _size(path: Path) -> int:
    # iterdir and rmtree do not traverse nested symbolic links. Their targets
    # contribute no freed bytes and are never removed.
    if path.is_symlink():
        return 0
    if path.is_dir():
        return sum(_size(child) for child in path.iterdir())
    return path.stat().st_size


def _plan(
    conn: sqlite3.Connection, paths: Paths, paper_ids: Iterable[str] | None,
) -> tuple[RemovalPreview, list[_File]]:
    preview = RemovalPreview()
    files: list[_File] = []
    ids = list(dict.fromkeys(paper_ids)) if paper_ids is not None else [
        r[0] for r in conn.execute('''SELECT id FROM papers p WHERE NOT EXISTS
                                    (SELECT 1 FROM project_papers pp WHERE pp.paper_id=p.id)
                                    ORDER BY id''')]
    # Evaluate the potentially nested JSON references once per operation.
    referenced = {r[0] for r in conn.execute('SELECT evidence_id FROM library_evidence_references')}
    for paper_id in ids:
        paper = conn.execute('SELECT id,title FROM papers WHERE id=?', (paper_id,)).fetchone()
        reason = None
        if paper is None:
            reason = 'paper not found'
        elif conn.execute('SELECT 1 FROM project_papers WHERE paper_id=? LIMIT 1', (paper_id,)).fetchone():
            reason = 'belongs to a project'
        else:
            spans = {r[0] for r in conn.execute('SELECT id FROM evidence_spans WHERE paper_id=?', (paper_id,))}
            foreign_link = conn.execute('''SELECT 1 FROM repro_links r JOIN evidence_spans e ON e.id=r.evidence_id
                                           WHERE e.paper_id=? AND r.paper_id<>? LIMIT 1''',
                                        (paper_id, paper_id)).fetchone()
            if referenced.intersection(spans) or foreign_link:
                reason = 'cited by an existing analysis'
        if reason:
            preview.refused.append(RemovalIssue(id=paper_id, reason=reason))
            continue
        assert paper is not None
        preview.papers.append(RemovalPaper(id=paper_id, title=paper['title']))
        for artifact in conn.execute('SELECT sha256, pdf_path FROM artifacts WHERE paper_id=?', (paper_id,)):
            sha = artifact['sha256']
            if not re.fullmatch(r'[0-9a-f]{64}', sha):
                preview.file_errors.append(FileIssue(path=str(sha), reason='invalid artifact SHA256; files kept'))
                continue
            # The stored original: <sha>.pdf, or a document with its own extension (.docx, .html…).
            suffix = Path(str(artifact['pdf_path'] or '')).suffix.lower()
            stored = paths.pdfs / f'{sha}{suffix if suffix in DOCUMENT_SUFFIXES else ".pdf"}'
            for path, parsed in ((stored, False), (paths.parsed / sha, True)):
                try:
                    _safe_path(paths, path)
                    if not path.exists():
                        continue
                    if parsed != path.is_dir():
                        raise OSError('unexpected library file type; kept')
                    size = _size(path)
                except OSError as exc:
                    preview.file_errors.append(FileIssue(path=str(path), reason=str(exc)))
                    continue
                files.append(_File(path, parsed, size))
                preview.pdf_count += int(not parsed)
                preview.parsed_folder_count += int(parsed)
                preview.bytes_freed += size
    preview.paper_count = len(preview.papers)
    return preview, files


def plan_removal(
    conn: sqlite3.Connection, paths: Paths, paper_ids: Iterable[str] | None = None,
) -> RemovalPreview:
    """Preview removable papers, refusals and existing canonical files; None selects all unused papers."""
    return _plan(conn, paths, paper_ids)[0]


def remove_papers(
    conn: sqlite3.Connection, paths: Paths, paper_ids: Iterable[str] | None = None,
) -> RemovalReport:
    """Recheck and remove eligible papers atomically, then collect files; see the module's audit rule."""
    conn.execute('BEGIN IMMEDIATE')
    staged: list[tuple[_File, Path]] = []
    trash_dir: Path | None = None
    try:
        preview, files = _plan(conn, paths, paper_ids)
        for paper in preview.papers:
            # _plan checked these spans against all JSON and relational references
            # once under this write lock. The trigger additionally checks live FKs.
            conn.execute('''INSERT INTO evidence_span_deletions
                            SELECT id FROM evidence_spans WHERE paper_id=?''', (paper.id,))
            conn.execute('INSERT INTO library_paper_deletions VALUES (?)', (paper.id,))
            # Repro links and non-cascading analysis FKs must be removed explicitly.
            conn.execute('DELETE FROM repro_links WHERE paper_id=?', (paper.id,))
            conn.execute('DELETE FROM evidence_spans WHERE paper_id=?', (paper.id,))
            conn.execute('DELETE FROM screening_history WHERE paper_id=?', (paper.id,))
            conn.execute('''DELETE FROM claim_reviews
                            WHERE claim_id IN (SELECT id FROM claims WHERE paper_id=?)''', (paper.id,))
            for table in ('claims', 'fulltext_screening', 'claim_extractions', 'matrix_cells'):
                conn.execute(f'DELETE FROM {table} WHERE paper_id=?', (paper.id,))
            # parse_runs -> sections/chunks/assets cascade; chunks_ad updates FTS5.
            conn.execute('DELETE FROM artifacts WHERE paper_id=?', (paper.id,))
            conn.execute('DELETE FROM papers WHERE id=?', (paper.id,))
        conn.execute('DELETE FROM evidence_span_deletions')
        conn.execute('DELETE FROM library_paper_deletions')
        if files:
            trash_dir = paths.root / 'library' / '.trash' / str(uuid4())
            _safe_path(paths, trash_dir)
            trash_dir.mkdir(parents=True)
            for file in files:
                _safe_path(paths, file.path)
                target = trash_dir / file.path.name
                file.path.rename(target)
                staged.append((file, target))
        conn.execute('COMMIT')
    except BaseException:
        try:
            # Restore while holding the write lock, before another importer can
            # commit a replacement at a canonical path.
            for file, target in reversed(staged):
                _safe_path(paths, file.path)
                _safe_path(paths, target)
                target.rename(file.path)
            if trash_dir is not None and trash_dir.exists():
                trash_dir.rmdir()
        finally:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
        raise
    report = RemovalReport(**preview.model_dump(), removed=[p.id for p in preview.papers])
    report.bytes_freed = 0
    report.pdf_count = 0
    report.parsed_folder_count = 0
    for file, target in staged:
        try:
            _safe_path(paths, target)
            if file.parsed:
                shutil.rmtree(target)
            else:
                target.unlink()
            report.bytes_freed += file.size
            report.pdf_count += int(not file.parsed)
            report.parsed_folder_count += int(file.parsed)
        except OSError as exc:
            report.file_errors.append(FileIssue(path=str(target), reason=str(exc)))
    if trash_dir is not None:
        try:
            _safe_path(paths, trash_dir)
            if not any(trash_dir.iterdir()):
                trash_dir.rmdir()
        except OSError as exc:
            report.file_errors.append(FileIssue(path=str(trash_dir), reason=str(exc)))
    return report
