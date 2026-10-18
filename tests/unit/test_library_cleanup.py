from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from litforge.analysis.ledger import record_spans
from litforge.analysis.runs import link_evidence, start_run
from litforge.cli import app
from litforge.config import Paths
from litforge.indexing.fts import search
from litforge.ingest import ingest_file
from litforge.library_cleanup import plan_removal, remove_papers
from litforge.storage.cache import sha256_file
from litforge.storage.db import open_db
from litforge.ui import api

SHA = 'a' * 64


def seed(conn: sqlite3.Connection, paths: Paths) -> str:
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('p','Unused','now','now')")
    conn.execute("""INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at)
                    VALUES ('a','p',?,'library/pdfs/ignored.pdf',3,'ok','now')""", (SHA,))
    conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,
                    output_dir,is_current,created_at)
                    VALUES ('r','a','fake','1','h','ok',?,1,'now')""", (f'library/parsed/{SHA}/v1',))
    conn.execute("""INSERT INTO sections(id,parse_run_id,kind,level,order_idx)
                    VALUES ('s','r','body',1,0)""")
    conn.execute("""INSERT INTO chunks(id,parse_run_id,section_id,paper_id,kind,order_idx,text,token_count,
                    page_start,page_end,hash) VALUES ('c','r','s','p','text',0,'zebrafinch cleanup',2,1,1,'h')""")
    conn.execute("INSERT INTO assets(id,parse_run_id,kind,page,quality) VALUES ('asset','r','figure',1,'verified')")
    conn.execute("INSERT INTO artifact_aliases VALUES ('a','/user/original.pdf','original.pdf','now')")
    (paths.pdfs / f'{SHA}.pdf').write_bytes(b'pdf')
    folder = paths.parsed / SHA / 'v1'
    folder.mkdir(parents=True)
    (folder / 'paper.md').write_bytes(b'parse')
    return record_spans(conn, search(conn, 'zebrafinch'))[0].id


def project(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('proj','Demo','now')")


def test_full_cleanup_and_repro_links(conn: sqlite3.Connection, paths: Paths) -> None:
    evidence = seed(conn, paths)
    conn.execute("INSERT INTO repro_links VALUES ('p','https://example.org','code',?)", (evidence,))
    conn.execute("INSERT INTO paper_references VALUES ('p','W1')")
    preview = plan_removal(conn, paths, None)
    assert [p.id for p in preview.papers] == ['p']
    assert (preview.paper_count, preview.pdf_count, preview.parsed_folder_count, preview.bytes_freed) == (1, 1, 1, 8)
    assert (paths.pdfs / f'{SHA}.pdf').exists()  # preview has no side effects
    report = remove_papers(conn, paths, ['p'])
    assert report.removed == ['p'] and not report.refused and not report.file_errors
    assert report.bytes_freed == 8
    for table in ('papers', 'artifacts', 'artifact_aliases', 'parse_runs', 'sections', 'chunks', 'assets',
                  'evidence_spans', 'repro_links', 'paper_references'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table
    assert conn.execute('SELECT rowid FROM chunks_fts').fetchall() == []
    assert conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'zebrafinch'").fetchall() == []
    conn.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES ('integrity-check',1)")
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert not (paths.pdfs / f'{SHA}.pdf').exists()
    assert not (paths.parsed / SHA).exists()


def test_membership_rechecked_after_preview(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    assert plan_removal(conn, paths, ['p']).paper_count == 1
    project(conn)
    conn.execute("INSERT INTO project_papers VALUES ('proj','p','manual','now')")
    report = remove_papers(conn, paths, ['p', 'missing'])
    assert report.removed == []
    assert 'project' in report.refused[0].reason
    assert 'not found' in report.refused[1].reason
    assert (paths.pdfs / f'{SHA}.pdf').exists()
    assert plan_removal(conn, paths, None).paper_count == 0


def test_cited_span_survives_even_without_project(conn: sqlite3.Connection, paths: Paths) -> None:
    evidence = seed(conn, paths)
    run = start_run(conn, 'ask', None, {}, {})
    spans = record_spans(conn, search(conn, 'zebrafinch'))
    link_evidence(conn, run, spans, [None])
    report = remove_papers(conn, paths, ['p'])
    assert report.removed == [] and report.refused[0].reason == 'cited by an existing analysis'
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        conn.execute('DELETE FROM evidence_spans WHERE id=?', (evidence,))
    assert conn.execute('SELECT text FROM evidence_spans').fetchone()[0] == 'zebrafinch cleanup'


def test_unreferenced_spans_still_require_cleanup_authorization(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        conn.execute('DELETE FROM evidence_spans')


@pytest.mark.parametrize('reference', ['run_evidence', 'claim_evidence', 'repro_links'])
def test_relational_references_block_even_authorized_span_deletion(conn: sqlite3.Connection, paths: Paths,
                                                                 reference: str) -> None:
    evidence = seed(conn, paths)
    if reference == 'repro_links':
        conn.execute("INSERT INTO repro_links VALUES ('p','https://example.org','code',?)", (evidence,))
    else:
        project(conn)
        run = start_run(conn, 'ask', 'proj', {}, {})
        if reference == 'run_evidence':
            conn.execute('INSERT INTO run_evidence(run_id,evidence_id,rank) VALUES (?,?,1)', (run, evidence))
        else:
            conn.execute("""INSERT INTO claims(id,project_id,paper_id,text,canonical_text,canonical_hash,
                            type,polarity,run_id,model,prompt_version,created_at)
                            VALUES ('claim','proj','p','Claim','claim','h','finding','positive',?,'fake','v','now')""",
                         (run,))
            conn.execute("INSERT INTO claim_evidence VALUES ('claim',?,'supports','direct')", (evidence,))
    conn.execute('INSERT INTO evidence_span_deletions VALUES (?)', (evidence,))
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        conn.execute('DELETE FROM evidence_spans WHERE id=?', (evidence,))
    assert conn.execute('SELECT COUNT(*) FROM evidence_spans').fetchone()[0] == 1


def test_bulk_cleanup_evaluates_json_references_once(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    conn.execute('BEGIN')
    conn.executemany('''INSERT INTO evidence_spans(id,paper_id,parse_run_id,chunk_id,parser,parser_version,
                        kind,section,page,page_start,page_end,bbox_json,text,content_hash,created_at)
        SELECT ?,paper_id,parse_run_id,chunk_id,parser,parser_version,kind,section,page,page_start,page_end,
               bbox_json,text,content_hash,created_at FROM evidence_spans LIMIT 1''',
                     [(f'span-{i}',) for i in range(4999)])
    conn.executemany('''INSERT INTO analysis_runs(id,task,input_hash,status,config_json,result_json,started_at)
        VALUES (?,'ask','h','ok',?,?,'now')''',
                     [(f'run-{i}', json.dumps({'run': i}),
                       json.dumps({'results': [{'text': f'unrelated-{i}-{j}'} for j in range(10)]}))
                      for i in range(2000)])
    conn.execute('COMMIT')
    started = perf_counter()
    # Interrupt a regression instead of letting the quadratic query run for minutes.
    conn.set_progress_handler(lambda: int(perf_counter() - started >= 5), 10000)
    try:
        report = remove_papers(conn, paths, ['p'])
    finally:
        conn.set_progress_handler(None, 0)
    assert perf_counter() - started < 5
    assert report.removed == ['p']
    assert conn.execute('SELECT COUNT(*) FROM evidence_spans').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM evidence_span_deletions').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM library_paper_deletions').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0] == 2000


@pytest.mark.parametrize('location', ['model_calls', 'analysis_runs', 'answer_sentences', 'project_snapshots',
                                      'human_reviews', 'gap_candidates', 'run_result', 'quotes', 'encoded_review'])
def test_json_references_protect_span(conn: sqlite3.Connection, paths: Paths, location: str) -> None:
    evidence = seed(conn, paths)
    project(conn)
    run = start_run(conn, 'ask', 'proj', {}, {})
    ids = json.dumps([evidence])
    if location == 'model_calls':
        conn.execute("""INSERT INTO model_calls(id,provider,model,task,input_hash,evidence_ids_json,status,created_at)
                        VALUES ('m','local','fake','ask','h',?,'ok','now')""", (ids,))
    elif location == 'analysis_runs':
        conn.execute('UPDATE analysis_runs SET dropped_evidence_ids_json=? WHERE id=?', (ids, run))
    elif location in ('answer_sentences', 'quotes'):
        conn.execute("""INSERT INTO answer_sentences(id,run_id,idx,text,kind,evidence_ids_json,quotes_json,created_at)
                        VALUES ('s',?,0,'Answer','reported_fact',?,?,'now')""",
                     (run, ids if location == 'answer_sentences' else '[]',
                      json.dumps([{'evidence_id': evidence}]) if location == 'quotes' else '[]'))
    elif location == 'project_snapshots':
        conn.execute("INSERT INTO project_snapshots VALUES ('snap','proj','','now','h',?)",
                     (json.dumps({'sentences': [{'evidence_ids': [evidence]}]}),))
    elif location in ('human_reviews', 'encoded_review'):
        conn.execute("INSERT INTO human_reviews VALUES ('review','sentence','old','edit',?,'{}','','now')",
                     (json.dumps({'evidence_ids_json': ids}) if location == 'encoded_review'
                      else json.dumps({'evidence_ids': [evidence]}),))
    elif location == 'gap_candidates':
        conn.execute("""INSERT INTO gap_candidates(id,project_id,kind,title,detail,source_key,evidence_ids_json,
                        created_at,updated_at) VALUES ('gap','proj','test','Gap','','key',?,'now','now')""", (ids,))
    elif location == 'run_result':
        # Nested references are also stored in run result payloads, not just evidence_ids_json columns.
        conn.execute('UPDATE analysis_runs SET result_json=? WHERE id=?',
                     (json.dumps({'causes': [{'evidence_ids': [evidence]}]}), run))
    preview = plan_removal(conn, paths, ['p'])
    assert preview.paper_count == 0 and preview.refused[0].reason == 'cited by an existing analysis'
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        conn.execute('DELETE FROM evidence_spans')
    assert not remove_papers(conn, paths, ['p']).removed


def test_paths_cannot_escape_or_touch_exports(conn: sqlite3.Connection, paths: Paths, tmp_path: Path) -> None:
    seed(conn, paths)
    outside = tmp_path / 'outside'
    outside.mkdir()
    original = outside / 'original.pdf'
    original.write_text('mine')
    conn.execute('UPDATE artifacts SET pdf_path=?', (str(original),))
    conn.execute('UPDATE parse_runs SET output_dir=?', (str(outside),))
    (paths.parsed / SHA / 'external').symlink_to(outside, target_is_directory=True)
    exports = paths.root / 'exports'
    exports.mkdir()
    (exports / 'note.md').write_text('export')
    report = remove_papers(conn, paths, ['p'])
    assert report.removed == ['p']
    assert original.read_text() == 'mine' and (exports / 'note.md').read_text() == 'export'


@pytest.mark.parametrize('kind', ['pdf', 'parsed', 'parent', 'hash'])
def test_symlink_or_malicious_hash_never_deletes_outside(conn: sqlite3.Connection, paths: Paths,
                                                        tmp_path: Path, kind: str) -> None:
    seed(conn, paths)
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / f'{SHA}.pdf'
    victim.write_text('mine')
    if kind == 'pdf':
        target = paths.pdfs / f'{SHA}.pdf'
        target.unlink()
        target.symlink_to(victim)
    elif kind == 'parsed':
        target = paths.parsed / SHA
        import shutil
        shutil.rmtree(target)
        target.symlink_to(outside, target_is_directory=True)
    elif kind == 'parent':
        (paths.pdfs / f'{SHA}.pdf').unlink()
        paths.pdfs.rmdir()
        paths.pdfs.symlink_to(outside, target_is_directory=True)
    else:
        conn.execute("UPDATE artifacts SET sha256='../../outside'")
    report = remove_papers(conn, paths, ['p'])
    assert report.removed == ['p'] and report.file_errors
    assert victim.read_text() == 'mine'


def test_rollback_keeps_files(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    conn.execute("CREATE TRIGGER fail_cleanup BEFORE DELETE ON papers BEGIN SELECT RAISE(ABORT,'blocked'); END")
    with pytest.raises(sqlite3.IntegrityError, match='blocked'):
        remove_papers(conn, paths, ['p'])
    assert conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] == 1
    assert conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'zebrafinch'").fetchall()
    assert (paths.pdfs / f'{SHA}.pdf').exists() and (paths.parsed / SHA).exists()


@pytest.mark.parametrize('failure', ['rename', 'commit'])
def test_rollback_restores_staged_files(conn: sqlite3.Connection, paths: Paths, failure: str) -> None:
    seed(conn, paths)
    renamed: list[Path] = []
    rename = Path.rename

    def stage(path: Path, target: Path) -> Path:
        assert conn.in_transaction
        assert target.is_relative_to(paths.root / 'library' / '.trash')
        if failure == 'rename' and renamed:
            raise PermissionError('cannot stage')
        renamed.append(target)
        return rename(path, target)

    commit_attempted: list[bool] = []

    def authorize(action: int, arg1: str | None, arg2: str | None,
                  db: str | None, source: str | None) -> int:
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT':
            commit_attempted.append(not (paths.pdfs / f'{SHA}.pdf').exists()
                                    and not (paths.parsed / SHA).exists())
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    if failure == 'commit':
        conn.set_authorizer(authorize)
    try:
        # Restoration uses the same primitive; only intercept canonical source paths.
        with (
            patch.object(Path, 'rename', lambda p, target: rename(p, target)
                         if p.is_relative_to(paths.root / 'library' / '.trash') else stage(p, target)),
            pytest.raises((PermissionError, sqlite3.DatabaseError)),
        ):
            remove_papers(conn, paths, ['p'])
    finally:
        conn.set_authorizer(None)
    assert len(renamed) == (1 if failure == 'rename' else 2)
    if failure == 'commit':
        assert commit_attempted == [True]
    assert (paths.pdfs / f'{SHA}.pdf').read_bytes() == b'pdf'
    assert (paths.parsed / SHA / 'v1' / 'paper.md').read_bytes() == b'parse'
    assert not any(p.exists() for p in renamed)
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM evidence_spans').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM evidence_span_deletions').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM library_paper_deletions').fetchone()[0] == 0


def test_reimport_after_commit_survives_trash_collection(conn: sqlite3.Connection, paths: Paths,
                                                       corpus: Path) -> None:
    seed(conn, paths)
    source = corpus / 'single_column.pdf'
    sha = sha256_file(source)
    pdf = paths.pdfs / f'{sha}.pdf'
    parsed = paths.parsed / sha
    (paths.pdfs / f'{SHA}.pdf').rename(pdf)
    (paths.parsed / SHA).rename(parsed)
    conn.execute('UPDATE artifacts SET sha256=?', (sha,))
    unlink = Path.unlink
    imported: list[str | None] = []

    def collect(path: Path, *args: object, **kwargs: object) -> None:
        assert not conn.in_transaction
        if not imported:
            assert not pdf.exists() and not parsed.exists()
            with open_db(paths.db) as other:
                result = ingest_file(other, paths, source, None)
            assert result.status == 'new'
            imported.append(result.paper_id)
            parsed.mkdir(parents=True)
            (parsed / 'fresh.md').write_text('fresh parse')
        unlink(path, *args, **kwargs)

    with patch.object(Path, 'unlink', collect):
        report = remove_papers(conn, paths, ['p'])
    assert report.removed == ['p'] and not report.file_errors
    assert imported and imported[0] != 'p'
    assert pdf.read_bytes() == source.read_bytes()
    assert (parsed / 'fresh.md').read_text() == 'fresh parse'
    assert conn.execute('SELECT paper_id FROM artifacts WHERE sha256=?', (sha,)).fetchone()[0] == imported[0]


def test_file_failure_is_reported_after_commit(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    def fail(path: Path, *args: object, **kwargs: object) -> None:
        assert not conn.in_transaction
        assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0
        raise PermissionError('cannot delete')
    with patch.object(Path, 'unlink', fail):
        report = remove_papers(conn, paths, ['p'])
    assert report.removed == ['p'] and 'cannot delete' in report.file_errors[0].reason


def test_cli_preview_confirmation_and_explicit_ids(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    runner = CliRunner()
    args = ['--data-dir', str(paths.root), 'library', 'remove-unused', '--paper', 'p']
    result = runner.invoke(app, args, input='n\n')
    assert result.exit_code != 0 and '1 papers' in result.output and '1 PDFs' in result.output
    assert 'irreversible' in result.output.lower()
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 1
    result = runner.invoke(app, args + ['--paper', 'missing', '--yes'])
    assert result.exit_code == 0, result.output
    assert 'not found' in result.output and 'Removed 1' in result.output
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0


def test_api_library_unused_and_validation(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    assert api.library(conn)[0]['unused'] is True
    assert api.paper_detail(conn, paths, 'p', None)['unused'] is True
    assert api.library_removal_preview(conn, paths, ['p'])['paper_count'] == 1
    for invalid in ('p', [2], {}, False, None, []):
        with pytest.raises(ValueError, match='paper_ids'):
            api.library_remove(conn, paths, invalid)
    assert api.library_remove(conn, paths, ['p'])['removed'] == ['p']
    with open_db(paths.db) as other:
        assert other.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0


def test_api_all_unused_must_be_explicitly_true(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    for invalid in (False, None, 1, 'true', []):
        with pytest.raises(ValueError):
            api.library_remove(conn, paths, all_unused=invalid)
    assert api.library_remove(conn, paths, all_unused=True)['removed'] == ['p']


def test_other_owned_rows_cascade_and_unrelated_paper_survives(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn, paths)
    project(conn)
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('q','Kept','now','now')")
    conn.execute("INSERT INTO project_papers VALUES ('proj','q','manual','now')")
    run = start_run(conn, 'ask', 'proj', {}, {})
    conn.execute("""INSERT INTO discovery_runs(id,project_id,kind,status,started_at)
                    VALUES ('discovery','proj','search','ok','now')""")
    conn.execute("""INSERT INTO discovery_hits(run_id,paper_id,source,relation,seed_paper_id)
                    VALUES ('discovery','p','test','seed','p'), ('discovery','q','test','cited_by','p')""")
    conn.execute("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project) "
                 "VALUES ('discovery','p',1,1,1)")
    conn.execute("INSERT INTO paper_relations VALUES ('p','q','test','now')")
    conn.execute("INSERT INTO duplicate_candidates VALUES ('p','q','title','pending','now')")
    conn.execute("INSERT INTO paper_marks VALUES ('proj','p','relevant','now')")
    conn.execute("""INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,
                    prompt_version,criteria_hash,fingerprint,screened_at)
                    VALUES ('proj','p','include',1,'{}','fake','v','h','h','now')""")
    conn.execute("""INSERT INTO claims(id,project_id,paper_id,text,canonical_text,canonical_hash,type,polarity,
                    run_id,model,prompt_version,created_at)
                    VALUES ('claim','proj','p','Claim','claim','h','finding','positive',?,'fake','v','now')""", (run,))
    conn.execute("INSERT INTO claim_extractions VALUES ('proj','p','h',?)", (run,))
    conn.execute("INSERT INTO claim_reviews VALUES ('review','claim','edit','{}','{}','','now')")
    conn.execute("""INSERT INTO screening_history(id,project_id,paper_id,stage,decision,source,
                    criteria_hash,reason,created_at)
                    VALUES ('history','proj','p','S2','include','human','h','','now')""")
    conn.execute("""INSERT INTO fulltext_screening(project_id,paper_id,decision,score,result_json,evidence_ids_json,
                    model,prompt_version,criteria_hash,fingerprint,run_id,screened_at)
                    VALUES ('proj','p','include',1,'{}','[]','fake','v','h','h',?,'now')""", (run,))
    conn.execute("""INSERT INTO matrices(id,project_id,name,question,columns_json,created_at,updated_at)
                    VALUES ('matrix','proj','Matrix','Question','[]','now','now')""")
    conn.execute("""INSERT INTO matrix_cells(id,matrix_id,paper_id,column_key,column_hash,status,model,provider,
                    prompt_version,parse_run_id,cache_key,run_id,updated_at)
                    VALUES ('cell','matrix','p','key','h','not_reported','fake','local','v','r','h',?,'now')""", (run,))
    conn.execute("""INSERT INTO asset_analyses(id,asset_id,mode,result_json,model,prompt_version,run_id,created_at)
                    VALUES ('aa','asset','chart','{}','fake','v',?,'now')""", (run,))
    report = remove_papers(conn, paths)
    assert report.removed == ['p']
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        table = '"' + row['name'].replace('"', '""') + '"'
        if any(col['name'] == 'paper_id' for col in conn.execute(f'PRAGMA table_info({table})')):
            assert conn.execute(f'SELECT COUNT(*) FROM {table} WHERE paper_id=?', ('p',)).fetchone()[0] == 0, table
    assert conn.execute('SELECT COUNT(*) FROM claim_reviews').fetchone()[0] == 0
    for table in ('claims', 'claim_extractions', 'fulltext_screening', 'matrix_cells', 'asset_analyses',
                  'discovery_results', 'paper_relations', 'duplicate_candidates', 'paper_marks', 'screening_results'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table
    assert conn.execute('SELECT id FROM papers').fetchone()[0] == 'q'
    hit = conn.execute('SELECT * FROM discovery_hits').fetchone()
    assert hit['paper_id'] == 'q' and hit['seed_paper_id'] is None
    assert conn.execute('SELECT id FROM analysis_runs').fetchone()[0] == run
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
