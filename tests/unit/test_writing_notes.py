from __future__ import annotations

import pytest

from litforge.writing import documents as wd
from litforge.writing import notes as wn
from litforge.writing.documents import DocumentError
from tests.unit.test_writing_documents import env as _env

env = _env


def test_crud_isolation_cascade(env):
    conn, paths = env
    doc = wd.create_document(conn, paths, 'demo', 'one')
    other = wd.create_document(conn, paths, 'demo', 'two')
    note = wn.add(conn, doc, 'main.tex', 1, ' anchor ', ' body ')
    assert note['body'] == 'body' and note['anchor'] == 'anchor'
    second = wn.add(conn, other, 'main.tex', 1, '', 'other')
    for action in (lambda: wn.delete(conn, other, note['id']),
                   lambda: wn.update(conn, other, note['id'], 'main.tex', 2, '', 'changed')):
        with pytest.raises(LookupError):
            action()
    assert wn.move(conn, other, 'main.tex', [{'id': note['id'], 'line': 2}]) == 0
    changed = wn.update(conn, doc, note['id'], 'main.tex', 2, 'x' * 250, 'updated')
    assert changed['line'] == 2 and len(changed['anchor']) == 200
    wn.delete(conn, doc, note['id'])
    with pytest.raises(LookupError):
        wn.delete(conn, doc, note['id'])
    wn.add(conn, doc, 'main.tex', 1, '', 'discard')
    wd.delete_document(conn, paths, doc.id)
    assert conn.execute('SELECT COUNT(*) FROM writing_notes WHERE document_id=?', (doc.id,)).fetchone()[0] == 0
    assert wn.list_notes(conn, other, 'main.tex')[0]['id'] == second['id']


@pytest.mark.parametrize(('line', 'body', 'file'), [(0, 'x', 'main.tex'), (-1, 'x', 'main.tex'),
    (1.5, 'x', 'main.tex'), (True, 'x', 'main.tex'), (1, '', 'main.tex'), (1, '  ', 'main.tex'),
    (1, 'x' * 2001, 'main.tex'), (1, 'x', '../main.tex'), (1, 'x', 'references.bib')])
def test_validation(env, line, body, file):
    conn, paths = env
    doc = wd.create_document(conn, paths, 'demo', 'one')
    with pytest.raises(DocumentError):
        wn.add(conn, doc, file, line, '', body)


@pytest.mark.parametrize(('text', 'line', 'anchor', 'expected', 'moved', 'orphan'), [
    ('a\nb\nc', 2, 'b', 2, False, False), ('b\na\nc', 2, 'b', 1, True, False),
    ('a\nc\nb', 2, 'b', 3, True, False), ('b\na\nb', 2, 'b', 1, True, False),
    ('a\nc', 10, 'b', 2, False, True), ('', 10, 'b', 1, False, True),
    ('a\nc', 10, '', 10, False, False)])
def test_anchors(env, text, line, anchor, expected, moved, orphan):
    conn, paths = env
    doc = wd.create_document(conn, paths, 'demo', 'one')
    (doc.folder / 'main.tex').write_text(text)
    note = wn.add(conn, doc, 'main.tex', line, anchor, 'note')
    result = wn.list_notes(conn, doc, 'main.tex')[0]
    assert (result['line'], result['moved'], result['orphan']) == (expected, moved, orphan)
    assert conn.execute('SELECT line FROM writing_notes WHERE id=?', (note['id'],)).fetchone()[0] == expected


def test_move_and_sort(env):
    conn, paths = env
    doc = wd.create_document(conn, paths, 'demo', 'one')
    (doc.folder / 'other.tex').write_text('x')
    first = wn.add(conn, doc, 'main.tex', 4, '', 'first')
    second = wn.add(conn, doc, 'main.tex', 3, '', 'second')
    other = wn.add(conn, doc, 'other.tex', 1, '', 'other')
    assert wn.move(conn, doc, 'main.tex', [{'id': first['id'], 'line': 2, 'anchor': ''},
                                        {'id': other['id'], 'line': 2}]) == 1
    assert [n['id'] for n in wn.list_notes(conn, doc, 'main.tex')] == [first['id'], second['id']]
