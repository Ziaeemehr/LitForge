"""Private notes anchored to lines of document source."""

from __future__ import annotations

import sqlite3
from typing import Any

from litforge.storage.db import new_id, now, transaction
from litforge.writing.documents import Document, DocumentError, _lock, resolve_inside


def _file(doc: Document, file: str) -> tuple[str, list[str]]:
    path = resolve_inside(doc.folder, file)
    if path.suffix.lower() != '.tex':
        raise DocumentError('notes require a .tex file')
    if not path.is_file():
        raise LookupError('file not found')
    return path.relative_to(doc.folder.resolve()).as_posix(), path.read_text(encoding='utf-8').splitlines()


def _position(line: Any, anchor: str) -> tuple[int, str]:
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise DocumentError('line must be an integer >= 1')
    return line, anchor.strip()[:200]


def _note(conn: sqlite3.Connection, doc: Document, id: str) -> dict[str, Any]:
    row = conn.execute('SELECT * FROM writing_notes WHERE id=? AND document_id=?', (id, doc.id)).fetchone()
    if row is None:
        raise LookupError('note not found')
    fields = ('id', 'file', 'line', 'anchor', 'body', 'created_at', 'updated_at')
    return {k: row[k] for k in fields} | {'moved': False, 'orphan': False}


def add(conn: sqlite3.Connection, doc: Document, file: str, line: Any, anchor: str, body: str) -> dict[str, Any]:
    file, _ = _file(doc, file)
    line, anchor = _position(line, anchor)
    body = _body(body)
    id, ts = new_id(), now()
    conn.execute('INSERT INTO writing_notes VALUES (?,?,?,?,?,?,?,?)',
                 (id, doc.id, file, line, anchor, body, ts, ts))
    return _note(conn, doc, id)


def _body(body: str) -> str:
    body = body.strip()
    if not body:
        raise DocumentError('write a note or delete it')
    if len(body) > 2000:
        raise DocumentError('notes are limited to 2000 characters')
    return body


def update(conn: sqlite3.Connection, doc: Document, id: str, file: str, line: Any,
           anchor: str, body: str) -> dict[str, Any]:
    _note(conn, doc, id)
    file, _ = _file(doc, file)
    line, anchor = _position(line, anchor)
    conn.execute('UPDATE writing_notes SET file=?, line=?, anchor=?, body=?, updated_at=? WHERE id=? AND document_id=?',
                 (file, line, anchor, _body(body), now(), id, doc.id))
    return _note(conn, doc, id)


def delete(conn: sqlite3.Connection, doc: Document, id: str) -> None:
    _note(conn, doc, id)
    conn.execute('DELETE FROM writing_notes WHERE id=? AND document_id=?', (id, doc.id))


def list_notes(conn: sqlite3.Connection, doc: Document, file: str) -> list[dict[str, Any]]:
    with _lock(doc.id), transaction(conn, immediate=True):
        file, lines = _file(doc, file)
        ids = conn.execute('SELECT id FROM writing_notes WHERE document_id=? AND file=?', (doc.id, file)).fetchall()
        notes = []
        for row in ids:
            note = _note(conn, doc, row[0])
            line, anchor = note['line'], note['anchor']
            if anchor and not (line <= len(lines) and lines[line - 1].strip() == anchor):
                matches = [i for i, text in enumerate(lines, 1) if text.strip() == anchor]
                if matches:
                    note['line'] = min(matches, key=lambda i: (abs(i - line), i))
                    note['moved'] = True
                else:
                    note['orphan'] = True
                    note['line'] = min(line, max(1, len(lines)))
                conn.execute('UPDATE writing_notes SET line=? WHERE id=?', (note['line'], note['id']))
            notes.append(note)
        return sorted(notes, key=lambda n: (n['line'], n['created_at'], n['id']))


def move(conn: sqlite3.Connection, doc: Document, file: str, notes: list[dict[str, Any]]) -> int:
    file, _ = _file(doc, file)
    moved = 0
    with _lock(doc.id), transaction(conn, immediate=True):
        for note in notes:
            row = conn.execute('SELECT id FROM writing_notes WHERE id=? AND document_id=? AND file=?',
                               (note.get('id'), doc.id, file)).fetchone()
            if row is None:
                continue
            line, anchor = _position(note.get('line'), str(note.get('anchor') or ''))
            conn.execute('UPDATE writing_notes SET line=?, anchor=?, updated_at=? WHERE id=?',
                         (line, anchor, now(), row[0]))
            moved += 1
    return moved
