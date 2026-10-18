"""Regressions from the second paper-versions review, using real APIs and offline PDFs."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pymupdf
import pytest
from discovery_fakes import FakeTransport, ok
from lens_fakes import gateway, seed
from test_ui_lens import LensServer

from litforge.analysis.ask import _question_spans, plan_calls
from litforge.analysis.claims import paper_runs
from litforge.analysis.figures import _store_span
from litforge.analysis.matrix import MatrixColumn, create_matrix
from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.runs import start_run
from litforge.config import Config, Paths
from litforge.discovery import versions
from litforge.discovery.fetch import fetch_open_access
from litforge.discovery.http import HttpClient
from litforge.export.markdown import export_markdown
from litforge.storage.db import connect
from litforge.ui.api import lens_matrix
from litforge.ui.server import App


def paper(conn: sqlite3.Connection, name: str, title: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO projects(id,name,created_at) VALUES ('proj','demo','now')")
    conn.execute("INSERT INTO papers(id,title,doi,created_at,updated_at) VALUES (?,?,?,'now','now')",
                 (name, title or 'A study about ' + name, '10.5555/' + name))
    conn.execute("INSERT INTO project_papers(project_id,paper_id,source,added_at) VALUES ('proj',?,'manual','now')",
                 (name,))


def pdf_bytes(text: str, *, title: str | None = None, doi: str | None = None) -> bytes:
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        doc.set_metadata({'title': title or '', 'subject': doi or ''})
        return doc.tobytes()


def http(transport: FakeTransport) -> HttpClient:
    return HttpClient(transport=transport, sleep=lambda _: None, max_retries=0)


def two_versions(conn: sqlite3.Connection) -> None:
    seed(conn, 'pub')
    seed(conn, 'pre')
    versions.confirm_version_link(conn, 'pub', 'pre')
    versions.set_primary(conn, 'pub')


def test_matrix_api_preserves_ui_metadata(conn: sqlite3.Connection) -> None:
    seed(conn)
    matrix = create_matrix(conn, 'proj', 'Matrix', 'gain',
                           [MatrixColumn(key='gain', label='Gain', instruction='gain', type='number')])
    data = lens_matrix(conn, matrix.id)
    assert {'id', 'name', 'question', 'columns', 'cells', 'papers', 'warnings', 'evidence'} <= data.keys()
    assert data['id'] == matrix.id and data['columns'][0]['key'] == 'gain'


@pytest.mark.parametrize('title_matches', [True, False])
@pytest.mark.parametrize('requested_doi', [True, False])
def test_fetch_cited_doi_without_own_doi_never_moves_pdf(
    conn: sqlite3.Connection, paths: Paths, config: Config, title_matches: bool, requested_doi: bool,
) -> None:
    title = 'Neural mass parameters in synthetic populations'
    paper(conn, 'selected', title)
    paper(conn, 'cited', 'A distinct investigation of bird migration')
    if not requested_doi:
        conn.execute("UPDATE papers SET doi=NULL WHERE id='selected'")
    conn.execute("UPDATE papers SET oa_url='https://pub.example/selected.pdf' WHERE id='selected'")
    pdf_title = title if title_matches else 'An unrelated paper about quantum optics'
    body = pdf_bytes(pdf_title + '\nIntroduction cites doi:10.5555/cited\nResults: a new finding.')
    transport = FakeTransport([('pub.example/selected.pdf', ok(body))])
    report = fetch_open_access(conn, paths, config, http(transport), 'proj', only='all', paper_ids=['selected'])
    attached = [r[0] for r in conn.execute("SELECT paper_id FROM artifacts WHERE status='ok'")]
    assert attached == (['selected'] if title_matches else [])
    assert report.downloaded == int(title_matches)
    if not title_matches:
        assert report.failed and 'selected record was left unchanged' in report.items[0]['reason']


@pytest.mark.parametrize('signal', ['metadata', 'title'])
def test_fetch_positive_identity_can_attach_known_other_record(
    conn: sqlite3.Connection, paths: Paths, config: Config, signal: str,
) -> None:
    paper(conn, 'selected', 'Published neural population dynamics')
    other_title = 'A distinct investigation of bird migration'
    paper(conn, 'other', other_title)
    conn.execute("UPDATE papers SET oa_url='https://pub.example/selected.pdf' WHERE id='selected'")
    body = pdf_bytes(other_title + '\nDOI: 10.5555/other',
                     doi='10.5555/other' if signal == 'metadata' else None)
    report = fetch_open_access(conn, paths, config, http(FakeTransport([('selected.pdf', ok(body))])),
                               'proj', only='all', paper_ids=['selected'])
    assert [r[0] for r in conn.execute("SELECT paper_id FROM artifacts WHERE status='ok'")] == ['other']
    assert report.downloaded == 1 and report.failed


def test_fetch_gives_a_version_pdf_to_the_record_it_names_more_exactly(
    conn: sqlite3.Connection, paths: Paths, config: Config,
) -> None:
    # Real case: eLife record, bioRxiv preprint PDF; both titles pass the 0.92 similarity bar.
    published = ('Virtual Brain Inference (VBI), a flexible and integrative toolkit for efficient '
                 'probabilistic inference on whole-brain models')
    preprint = ('Virtual Brain Inference (VBI): A flexible and integrative toolkit for efficient '
                'probabilistic inference on virtual brain models')
    paper(conn, 'selected', published)
    paper(conn, 'other', preprint)
    conn.execute("UPDATE papers SET oa_url='https://pub.example/selected.pdf' WHERE id='selected'")
    versions.confirm_version_link(conn, 'selected', 'other')
    body = pdf_bytes(preprint + '\nAbstract\nhttps://doi.org/10.5555/other')
    report = fetch_open_access(conn, paths, config, http(FakeTransport([('selected.pdf', ok(body))])),
                               'proj', only='all', paper_ids=['selected'])
    assert [r[0] for r in conn.execute("SELECT paper_id FROM artifacts WHERE status='ok'")] == ['other']
    assert report.downloaded == 1


def test_similar_title_of_a_cited_paper_never_takes_the_pdf(
    conn: sqlite3.Connection, paths: Paths, config: Config,
) -> None:
    # The selected PDF omits its DOI, prints its own arXiv id and cites a library paper whose title is
    # closer to the PDF's than the registry title is: the PDF must stay with the selected record.
    paper(conn, 'selected', 'Amortised inference of neural mass models with normalising flows')  # 0.95 vs 1.0
    paper(conn, 'cited', 'Amortized inference for neural mass models with normalizing flows')
    conn.execute("UPDATE papers SET doi=NULL, arxiv_id='2401.01234',"
                 " oa_url='https://pub.example/selected.pdf' WHERE id='selected'")
    body = pdf_bytes('Amortized inference for neural mass models with normalizing flows\n'
                     'arXiv:2401.01234v2 [q-bio.NC]\nWe build on doi:10.5555/cited.')
    fetch_open_access(conn, paths, config, http(FakeTransport([('arxiv.org/pdf/2401.01234', ok(body))])),
                      'proj', only='all', paper_ids=['selected'])
    assert [r[0] for r in conn.execute("SELECT paper_id FROM artifacts WHERE status='ok'")] == ['selected']


def test_another_records_pdf_is_attached_there_and_the_next_candidate_is_tried(
    conn: sqlite3.Connection, paths: Paths, config: Config,
) -> None:
    paper(conn, 'selected', 'Published neural population dynamics')
    paper(conn, 'other', 'A distinct investigation of bird migration')
    conn.execute("DELETE FROM project_papers WHERE paper_id='other'")  # library only
    conn.execute("UPDATE papers SET oa_url='https://pub.example/first.pdf',"
                 " arxiv_id='2401.05555' WHERE id='selected'")
    wrong = pdf_bytes('A distinct investigation of bird migration\nDOI: 10.5555/other', doi='10.5555/other')
    right = pdf_bytes('Published neural population dynamics\nDOI: 10.5555/selected')
    transport = FakeTransport([('arxiv.org/pdf/2401.05555', ok(wrong)), ('pub.example/first.pdf', ok(right))])
    report = fetch_open_access(conn, paths, config, http(transport), 'proj', only='all', paper_ids=['selected'])
    owners = sorted(r[0] for r in conn.execute("SELECT paper_id FROM artifacts WHERE status='ok'"))
    assert owners == ['other', 'selected']
    assert conn.execute("SELECT COUNT(*) FROM project_papers WHERE paper_id='other'").fetchone()[0] == 0
    assert report.downloaded == 2


@pytest.mark.parametrize('via', ['landing', 'redirect'])
def test_fetch_labels_resolved_preprint_url(
    conn: sqlite3.Connection, paths: Paths, config: Config, via: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper(conn, 'selected')
    conn.execute("UPDATE papers SET openalex_id='W1' WHERE id='selected'")
    final_url = 'https://biorxiv.org/content/draftv2.full.pdf'
    locations = {'locations': [{'is_oa': True, 'landing_page_url': 'https://pub.example/article',
                                 'version': 'publishedVersion'}]}
    body = pdf_bytes('bioRxiv preprint\nThe selected paper doi:10.5555/selected')
    if via == 'landing':
        landing = f'<meta name="citation_pdf_url" content="{final_url}">'.encode()
        transport = FakeTransport([('api.openalex.org', ok(locations)),
                                   ('pub.example/article', ok(landing)), (final_url, ok(body))])
    else:
        # Exercise the real urllib transport's redirect URL propagation without a socket.
        import io
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__.return_value = response
        response.status, response.headers = 200, {}
        response.geturl.return_value = final_url
        response.read = io.BytesIO(body).read
        monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: response)
        from litforge.discovery.http import urllib_transport

        def transport(url, headers, timeout):
            return ok(locations) if 'api.openalex.org' in url else urllib_transport(url, headers, timeout)
    report = fetch_open_access(conn, paths, config, HttpClient(transport=transport, sleep=lambda _: None),
                               'proj', only='all')
    artifact = conn.execute('SELECT version,version_source,source_url,version_label FROM artifacts').fetchone()
    assert tuple(artifact) == ('submitted', 'host', final_url, 'bioRxiv preprint, v2')
    assert report.items[0]['version_source'] == 'host'


def test_transitive_explicit_links_respect_unlinked_pair(conn: sqlite3.Connection) -> None:
    for name in ['a', 'b', 'c', 'd']:
        paper(conn, name)
    versions.confirm_version_link(conn, 'a', 'b')
    versions.unlink_version(conn, 'a')
    versions.confirm_version_link(conn, 'c', 'd')
    relations = {'10.5555/b': [{'id-type': 'doi', 'id': '10.5555/c', 'relation': 'has-version'}]}
    assert versions.link_explicit_versions(conn, relations) == 1
    conn.execute("UPDATE papers SET openalex_id='W2' WHERE id IN ('a','d')")
    assert versions.link_explicit_versions(conn) == 0
    assert versions._group_for(conn, 'a') != versions._group_for(conn, 'b')
    assert versions._rejected(conn, 'a', 'b')


@pytest.mark.parametrize('all_versions,selection,expected', [
    (False, None, set()), (True, None, {'pre'}), (False, ['pre'], {'pre'}), (False, [], set()),
])
def test_ask_figure_fallback_obeys_version_selection(
    conn: sqlite3.Connection, all_versions: bool, selection: list[str] | None, expected: set[str],
) -> None:
    two_versions(conn)
    conn.execute("INSERT INTO assets(id,parse_run_id,kind,label,caption,page,quality) "
                 "VALUES ('figure-pre','rpre','figure','Figure 1','zebrafinch',1,'verified')")
    rid = start_run(conn, 'asset_analyze', 'proj', {}, {}, 'v1')
    conn.execute("INSERT INTO asset_analyses(id,asset_id,mode,result_json,model,prompt_version,run_id,created_at) "
                 "VALUES ('aa','figure-pre','chart','{}','fake','v1',?,'now')", (rid,))
    asset = conn.execute("SELECT * FROM assets WHERE id='figure-pre'").fetchone()
    p = conn.execute("SELECT 'pre' AS paper_id,'Title pre' AS title,'fake' AS parser,'1' AS parser_version").fetchone()
    _store_span(conn, asset, p, 'Digitized value: 99', 'figure_estimate', True, 'point')
    hits = retrieve_for_question(conn, 'zebrafinch', 'proj', all_versions=all_versions, paper_ids=selection)
    assert hits == []
    # Exercise public planning as well: its fallback must receive all_versions.
    gw, _ = gateway(conn)
    previews = plan_calls(conn, gw, 'zebrafinch', 'proj', paper_ids=selection, all_versions=all_versions)
    assert bool(previews) == bool(expected)
    if not all_versions:
        assert {s.paper_id for s in _question_spans(conn, hits, 'zebrafinch', 'proj', selection)} == expected


def test_primary_prefers_indexed_current_parse_over_published_pdf(conn: sqlite3.Connection) -> None:
    seed(conn, 'pre')
    conn.execute("UPDATE papers SET doi='10.1101/2024.01.01.123456' WHERE id='pre'")
    paper(conn, 'pub')
    conn.execute("INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at) "
                 "VALUES ('apub','pub','pubsha','publisher.pdf',1,'ok','now')")
    versions.confirm_version_link(conn, 'pub', 'pre')
    assert versions.primary_paper_ids(conn, 'proj') == {'pre'}
    assert {h.paper_id for h in retrieve_for_question(conn, 'gain', 'proj')} == {'pre'}
    assert set(paper_runs(conn, 'proj')) == {'pre'}
    # A parse with no output, stale chunks, or only whitespace is not usable.
    for sql in ["UPDATE parse_runs SET output_dir=NULL", "UPDATE parse_runs SET is_current=0",
                "UPDATE chunks SET text='   '"]:
        conn.execute(sql)
        assert versions.primary_paper_ids(conn, 'proj') == {'pub'}
        conn.execute("UPDATE parse_runs SET output_dir='parsed',is_current=1")
    conn.execute("UPDATE chunks SET text='Results: gain 3.25'")
    assert versions.primary_paper_ids(conn, 'proj') == {'pre'}
    versions.set_primary(conn, 'pub')
    assert versions.primary_paper_ids(conn, 'proj') == {'pub'}


def test_export_keeps_explicit_non_primary_selection(conn: sqlite3.Connection, paths: Paths, config: Config) -> None:
    two_versions(conn)
    conn.execute('UPDATE parse_runs SET output_dir=NULL')
    root, count = export_markdown(conn, paths, 'demo', 'proj', paper_ids=['pre'], config=config)
    assert count == 1
    assert 'paper_id: pre' in next((root / 'knowledge_base').glob('*.md')).read_text()


def test_synchronous_export_passes_all_versions(conn: sqlite3.Connection, paths: Paths, config: Config) -> None:
    two_versions(conn)
    conn.execute('UPDATE parse_runs SET output_dir=NULL')
    _, fake = gateway(conn)
    server = LensServer(App(paths, config), fake, '', '')
    status, result = server.call('/api/export', {'project': 'demo', 'all_versions': True})
    assert status == 200 and result['papers'] == 2


@pytest.mark.parametrize('registry', ['openalex', 'unpaywall'])
@pytest.mark.parametrize('declared', ['publishedVersion', 'acceptedVersion'])
def test_ranking_preprint_host_overrides_registry(registry: str, declared: str) -> None:
    locations = [{'is_oa': True, 'pdf_url': 'https://biorxiv.org/content/draft.pdf',
                   'url_for_pdf': 'https://biorxiv.org/content/draft.pdf', 'version': declared},
                 {'is_oa': True, 'pdf_url': 'https://pub.example/final.pdf',
                   'url_for_pdf': 'https://pub.example/final.pdf', 'version': 'publishedVersion'}]
    candidates = versions.rank_candidates(None, None, **{registry + '_locations': locations})
    assert [(c.version, c.version_source) for c in candidates] == [('published', registry), ('submitted', 'host')]
    assert candidates[0].url == 'https://pub.example/final.pdf'


def test_fetch_retries_after_identity_mismatch(conn: sqlite3.Connection, paths: Paths, config: Config) -> None:
    paper(conn, 'selected')
    conn.execute("UPDATE papers SET openalex_id='W1' WHERE id='selected'")
    locations = {'locations': [{'is_oa': True, 'version': 'publishedVersion', 'pdf_url': url}
                               for url in ['https://pub.example/stale.pdf', 'https://pub.example/correct.pdf']]}
    transport = FakeTransport([('api.openalex.org', ok(locations)),
                               ('stale.pdf', ok(pdf_bytes('Wrong paper doi:10.5555/wrong'))),
                               ('correct.pdf', ok(pdf_bytes('Correct paper doi:10.5555/selected')))])
    report = fetch_open_access(conn, paths, config, http(transport), 'proj', only='all')
    assert report.downloaded == 1 and not report.failed
    assert any('correct.pdf' in url for url in transport.calls)
    assert conn.execute('SELECT source_url FROM artifacts').fetchone()[0].endswith('correct.pdf')


@pytest.mark.parametrize('operation', ['link', 'confirm', 'unlink', 'reject', 'primary'])
def test_version_writes_reserve_wal_writer_before_reading(
    conn: sqlite3.Connection, paths: Paths, operation: str,
) -> None:
    paper(conn, 'a')
    paper(conn, 'b')
    if operation == 'unlink':
        versions.confirm_version_link(conn, 'a', 'b')
    if operation == 'reject':
        conn.execute("UPDATE papers SET title='A long study of neural dynamics and rhythms'")
        versions.suggest_similar_versions(conn)
    other = connect(paths.db)
    other.execute('PRAGMA busy_timeout=0')
    outcomes = []

    def trace(sql: str) -> None:
        if not outcomes and sql.lstrip().startswith('SELECT'):
            try:
                other.execute("UPDATE papers SET abstract='a concurrent job' WHERE id='a'")
            except sqlite3.OperationalError as exc:
                outcomes.append(str(exc))
            else:
                outcomes.append('committed')
    conn.set_trace_callback(trace)
    try:
        if operation == 'link':
            versions.link_explicit_versions(conn, {'10.5555/a': [
                {'id-type': 'doi', 'id': '10.5555/b', 'relation': 'has-version'}]})
        elif operation == 'confirm':
            versions.confirm_version_link(conn, 'a', 'b')
        elif operation == 'unlink':
            versions.unlink_version(conn, 'a')
        elif operation == 'reject':
            versions.reject_version_link(conn, 'a', 'b')
        else:
            versions.set_primary(conn, 'a')
    finally:
        conn.set_trace_callback(None)
        other.close()
    assert outcomes == ['database is locked']


@pytest.mark.parametrize('decision', ['suggest', 'reject', 'confirm', 'unlink'])
def test_suggestions_recheck_pair_after_writer_acquired(
    conn: sqlite3.Connection, paths: Paths, decision: str,
) -> None:
    paper(conn, 'a')
    paper(conn, 'b')
    conn.execute("UPDATE papers SET title='A long study of neural dynamics and rhythms'")
    other = connect(paths.db)
    fired = False
    def trace(sql: str) -> None:
        nonlocal fired
        if sql.strip().startswith('BEGIN') and not fired:
            fired = True
            if decision in {'suggest', 'reject'}:
                versions.suggest_similar_versions(other)
                if decision == 'reject':
                    versions.reject_version_link(other, 'a', 'b')
            else:
                versions.confirm_version_link(other, 'a', 'b')
                if decision == 'unlink':
                    versions.unlink_version(other, 'a')
    conn.set_trace_callback(trace)
    try:
        assert versions.suggest_similar_versions(conn) == 0
    finally:
        conn.set_trace_callback(None)
        other.close()
    if decision == 'suggest':
        assert conn.execute('SELECT COUNT(DISTINCT group_id) FROM paper_versions').fetchone()[0] == 1
        assert versions.reject_version_link(conn, 'a', 'b')
    assert conn.execute('SELECT COUNT(*) FROM paper_versions WHERE confirmed=0 AND rejected=0').fetchone()[0] == 0


@pytest.mark.parametrize('insert', ['INSERT', 'INSERT OR IGNORE', 'INSERT OR REPLACE'])
def test_suggestion_pair_uniqueness_is_enforced_by_database(conn: sqlite3.Connection, insert: str) -> None:
    paper(conn, 'a')
    paper(conn, 'b')
    conn.execute("UPDATE papers SET title='A long study of neural dynamics and rhythms'")
    assert versions.suggest_similar_versions(conn) == 1
    with pytest.raises(sqlite3.IntegrityError), versions.transaction(conn):
        for name in ['b', 'a']:  # reversed order is still the same pair
            conn.execute(f"{insert} INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at) "
                         "VALUES ('duplicate',?,'unknown','similarity:title-authors-year',0,'now')", (name,))


def test_pair_migration_upgrades_v24_and_preserves_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.storage import db

    migrations = db.available_migrations()
    with monkeypatch.context() as patch:
        patch.setattr(db, 'available_migrations', lambda: [m for m in migrations if m[0] <= 24])
        legacy = db.open_db(tmp_path / 'v24.sqlite')
    try:
        paper(legacy, 'a')
        paper(legacy, 'b')
        for group, rejected in [('pending1', 0), ('rejected', 1), ('pending2', 0)]:
            for name in ['a', 'b']:
                legacy.execute('INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,rejected,'
                               "created_at) VALUES (?,?,'unknown','test',0,?,'now')", (group, name, rejected))
        assert db.migrate(legacy) == ['0025_version_pairs.sql']
        assert legacy.execute('SELECT DISTINCT group_id FROM paper_versions').fetchall()[0][0] == 'rejected'
        assert legacy.execute('SELECT COUNT(*) FROM paper_versions').fetchone()[0] == 2
        assert versions._rejected(legacy, 'a', 'b')
        assert versions.suggest_similar_versions(legacy) == 0
        assert db.migrate(legacy) == []
        # Confirming a pair removes the pending-pair constraint; unlinking records the pair again.
        assert versions.confirm_version_link(legacy, 'a', 'b')
        assert versions.unlink_version(legacy, 'a')
        assert versions._rejected(legacy, 'a', 'b')
    finally:
        legacy.close()


def test_fetch_reference_title_is_not_first_page_title(
    conn: sqlite3.Connection, paths: Paths, config: Config,
) -> None:
    paper(conn, 'selected', 'Neural mass parameters in synthetic populations')
    cited_title = 'A distinct investigation of bird migration'
    paper(conn, 'cited', cited_title)
    conn.execute("UPDATE papers SET oa_url='https://pub.example/selected.pdf' WHERE id='selected'")
    body = pdf_bytes('An unrelated paper about quantum optics\nIntroduction\n' + cited_title + '\nDOI: 10.5555/cited')
    report = fetch_open_access(conn, paths, config, http(FakeTransport([('selected.pdf', ok(body))])),
                               'proj', only='all', paper_ids=['selected'])
    assert report.downloaded == 0
    assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE status='ok'").fetchone()[0] == 0
    assert 'selected record was left unchanged' in report.items[0]['reason']


def test_unlink_replaces_pending_pair_already_inside_confirmed_group(conn: sqlite3.Connection) -> None:
    for name in ['a', 'b', 'c']:
        paper(conn, name)
    conn.execute("UPDATE papers SET title='A long study of neural dynamics and rhythms'")
    assert versions.suggest_similar_versions(conn) == 3
    assert versions.confirm_version_link(conn, 'a', 'b')
    assert versions.confirm_version_link(conn, 'b', 'c')
    assert versions.unlink_version(conn, 'a')
    assert versions._rejected(conn, 'a', 'b') and versions._rejected(conn, 'a', 'c')
