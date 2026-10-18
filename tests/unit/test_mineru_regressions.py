from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from litforge.config import Config, LlamaCppServerConfig, Paths
from litforge.indexing.chunk import chunk_document
from litforge.ingest import ingest_paths
from litforge.parsing import mineru_parser as mineru
from litforge.parsing.docling_parser import DoclingParser
from litforge.parsing.run import artifacts_for_project
from litforge.ui import api


def middle(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {'schema': 'docvortex.middle', 'schema_version': '2.0',
            'extensions': {'docvortex_layout': {'pages': [
                {'page_idx': 0, 'width_pt': 600, 'height_pt': 800},
                {'page_idx': 1, 'width_pt': 600, 'height_pt': 800}]}},
            'pages': [{'page_idx': 0, 'blocks': blocks}, {'page_idx': 1, 'blocks': []}]}


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal and process inspection')
@pytest.mark.parametrize('ignore_term', [False, True])
def test_timeout_reaps_process_tree(tmp_path: Path, ignore_term: bool) -> None:
    pidfile = tmp_path / 'grandchild.pid'
    code = ("import os, signal, time; "
            + ('signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if ignore_term else '')
            + f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)")
    parent = f'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", {code!r}]); time.sleep(60)'
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            mineru._run_command([sys.executable, '-c', parent], env=dict(os.environ), timeout=0.5)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            stat = Path(f'/proc/{pid}/stat')
            if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                break
            time.sleep(0.05)
        else:
            pytest.fail('grandchild survived MinerU timeout')
    finally:
        if pidfile.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_output_capture_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_popen = subprocess.Popen
    def popen(*args: Any, **kwargs: Any) -> Any:
        assert kwargs.get('stdout') == subprocess.DEVNULL
        assert 'capture_output' not in kwargs
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(mineru.subprocess, 'Popen', popen)
    code = "import os; os.write(1, b'x' * 12_000_000); os.write(2, b'y' * 12_000_000 + b'FINAL TAIL')"
    result = mineru._run_command([sys.executable, '-c', code], env=dict(os.environ), timeout=10, out_dir=tmp_path)
    assert result.returncode == 0
    assert not result.stdout
    assert result.stderr.endswith('FINAL TAIL')
    assert len(result.stderr.encode()) <= 65536
    assert list(tmp_path.iterdir()) == []


def test_windows_timeout_kills_job_after_group_leader_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    class Process:
        pid = 123
        returncode = None
        def wait(self, timeout: float | None = None) -> int:
            if not events:
                events.append('timeout')
                raise subprocess.TimeoutExpired('mineru', timeout)
            return 0  # The leader exits on CTRL_BREAK; descendants still need killing.
        def send_signal(self, sig: int) -> None:
            assert sig == 999
            events.append('break')
    @contextmanager
    def job(process: Any) -> Iterator[Any]:
        assert process.pid == 123
        try:
            yield lambda: events.append('kill tree')
        finally:
            events.append('close job')
    def popen(*args: Any, **kwargs: Any) -> Process:
        assert kwargs['creationflags'] == 512
        assert 'start_new_session' not in kwargs
        return Process()
    monkeypatch.setattr(mineru, '_windows_job', job)
    monkeypatch.setattr(mineru, 'os', SimpleNamespace(name='nt', SEEK_END=os.SEEK_END))
    monkeypatch.setattr(mineru.subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, raising=False)
    monkeypatch.setattr(mineru.signal, 'CTRL_BREAK_EVENT', 999, raising=False)
    monkeypatch.setattr(mineru.subprocess, 'Popen', popen)
    monkeypatch.setattr(mineru.time, 'sleep', lambda _: None)
    with pytest.raises(subprocess.TimeoutExpired):
        mineru._run_command(['mineru'], env={}, timeout=0.5)
    assert events == ['timeout', 'break', 'kill tree', 'close job']


def test_table_spans_and_indexed_line_break(conn: sqlite3.Connection, paths: Paths, corpus: Path) -> None:
    html = ('<table><tr><th rowspan="2">Method</th><th colspan="3">Two Moons</th>'
            '<th colspan="3">Bernoulli GLM</th><th colspan="3">SIR</th></tr><tr>'
            + '<td>10,000</td><td>20,000</td><td>30,000</td>' * 3 + '</tr><tr><td>NPE</td>'
            + ''.join(f'<td>{i}</td>' for i in range(1, 10)) + '</tr><tr><td>Short</td></tr></table>')
    doc = mineru.map_middle_json(middle([{'type': 'table', 'content': [
        {'type': 'table_caption', 'content': 'Table 1: Runtime in seconds.'},
        {'type': 'table_body', 'content': html}]}]), 'test')
    expected = ['Method', *(['Two Moons'] * 3), *(['Bernoulli GLM'] * 3), *(['SIR'] * 3)]
    paragraph = doc.sections[0].paragraphs[0]
    assert paragraph.text.splitlines()[2].split(' | ') == expected
    assert paragraph.text.splitlines()[3].split(' | ')[0] == 'Method'
    assert paragraph.text.splitlines()[4].split(' | ') == ['NPE', *map(str, range(1, 10))]
    assert all(len(row.split(' | ')) == 10 for row in paragraph.text.splitlines()[2:])
    assert doc.assets[0].data is not None
    rows = str(doc.assets[0].data['markdown']).splitlines()
    assert all(len(row.split('|')) == 12 for row in rows)
    assert chunk_document(doc, 'k')[0].text == paragraph.text.rstrip()
    from litforge.indexing.fts import index_run
    from litforge.parsing.canonical import write_outputs
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    artifact = artifacts_for_project(conn, None)[0]
    out = paths.root / 'test-output'
    out.mkdir()
    write_outputs(doc, out)
    conn.execute("INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,created_at,output_dir)"
                 "VALUES ('table',?,'mineru','test','test','ok','now','test-output')", (artifact['id'],))
    row = conn.execute('SELECT r.*,a.paper_id,a.sha256 FROM parse_runs r JOIN artifacts a '
                       "ON a.id=r.artifact_id WHERE r.id='table'").fetchone()
    index_run(conn, paths, Config(), row)
    assert conn.execute("SELECT text FROM chunks WHERE parse_run_id='table'").fetchone()[0] == paragraph.text.rstrip()


@pytest.mark.parametrize('prefix', ['Figure 2:', 'Fig. 2:'])
@pytest.mark.parametrize('asset_kind', ['chart', 'image'])
def test_following_figure_caption(prefix: str, asset_kind: str) -> None:
    caption = prefix + ' Synthetic result.'
    doc = mineru.map_middle_json(middle([{'type': asset_kind, 'content': []},
                                       {'type': 'text', 'content': caption}]), 'test')
    assert doc.assets[0].caption == caption
    assert doc.sections[0].paragraphs[0].kind == 'caption'
    assert doc.full_text().count(caption) == 1


@pytest.mark.parametrize('separation', ['text', 'page', 'heading', 'table'])
def test_caption_requires_immediately_preceding_figure(separation: str) -> None:
    data = middle([{'type': 'chart', 'content': []}])
    caption = {'type': 'text', 'content': 'Figure 3: Standalone text.'}
    if separation == 'page':
        data['pages'][1]['blocks'].append(caption)
    else:
        kind = 'paragraph_title' if separation == 'heading' else separation
        data['pages'][0]['blocks'] += [{'type': kind, 'content': 'Intervening content'}, caption]
    doc = mineru.map_middle_json(data, 'test')
    assert doc.assets[0].caption is None
    assert doc.sections[-1].paragraphs[-1].kind == 'text'


@pytest.mark.parametrize(('heading', 'expected'), [
    ('A Appendix A: Tasks', 'appendix'), ('Appendix B', 'appendix'),
    ('B Supplementary material', 'appendix'), ('2. Supplementary details', 'appendix'),
    ('3.1 Appendix: Details', 'appendix'), ('Methods for appendix detection', 'body'),
])
@pytest.mark.parametrize('adapter', ['mineru', 'docling'])
def test_appendix_heading(heading: str, expected: str, adapter: str, tmp_path: Path,
                         monkeypatch: pytest.MonkeyPatch) -> None:
    if adapter == 'mineru':
        doc = mineru.map_middle_json(middle([{'type': 'paragraph_title', 'content': heading},
                                           {'type': 'text', 'content': 'Details'}]), 'test')
    else:
        ddoc = SimpleNamespace(pages={}, iterate_items=lambda: iter([
            (SimpleNamespace(label='section_header', text=heading), 1),
            (SimpleNamespace(label='text', text='Details'), 1)]))
        module = ModuleType('docling.document_converter')
        module.DocumentConverter = lambda: SimpleNamespace(convert=lambda _: SimpleNamespace(document=ddoc))
        monkeypatch.setitem(sys.modules, 'docling', ModuleType('docling'))
        monkeypatch.setitem(sys.modules, 'docling.document_converter', module)
        monkeypatch.setattr(DoclingParser, 'version', 'test')
        doc = DoclingParser().parse(tmp_path / 'unused.pdf', tmp_path, Config().parsing)
    assert doc.sections[0].kind == expected


@pytest.mark.parametrize('failure', ['failed', 'timeout', 'interrupt'])
@pytest.mark.parametrize('step', [1, 2])
def test_install_cleans_only_created_directory(paths: Paths, monkeypatch: pytest.MonkeyPatch,
                                              failure: str, step: int) -> None:
    target = paths.root / 'tools/mineru'
    calls: list[Any] = []
    def command(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args[0])
        (target / 'partial').write_text('partial download')
        if len(calls) < step:
            return subprocess.CompletedProcess(args[0], 0, '', '')
        if failure == 'interrupt':
            raise KeyboardInterrupt
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('install', kwargs['timeout'])
        return subprocess.CompletedProcess(args[0], 1, '', 'broken install')
    monkeypatch.setattr(mineru, '_run_command', command)
    with pytest.raises((RuntimeError, KeyboardInterrupt)):
        mineru.install_mineru(paths, timeout_s=0.5)
    assert not target.exists()
    target.mkdir()
    sentinel = target / 'user-command'
    sentinel.write_text('keep')
    with pytest.raises((RuntimeError, FileExistsError)):
        mineru.install_mineru(paths, timeout_s=0.5)
    assert len(calls) == step
    assert sentinel.read_text() == 'keep'


def test_install_total_deadline(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    timeouts: list[float] = []
    def command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs['timeout'])
        now[0] += 20
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(mineru, '_run_command', command)
    monkeypatch.setattr(mineru.time, 'monotonic', lambda: now[0])
    mineru.install_mineru(paths, timeout_s=60)
    assert timeouts == [60, 40]
    assert (paths.root / 'tools/mineru').is_dir()


@pytest.mark.parametrize('history', [False, True])
def test_ui_estimate_uses_successful_library_history(conn: sqlite3.Connection, paths: Paths, corpus: Path,
                                                   history: bool) -> None:
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    artifact = artifacts_for_project(conn, None)[0]
    if history:
        # All successful runs count, including historical runs. Ignore failed/other/invalid measurements.
        for i, (parser, status, elapsed, pages) in enumerate([
            ('mineru', 'ok', 60000, 2), ('mineru', 'ok', 240000, 4),
            ('mineru', 'failed', 999999, 1), ('pymupdf', 'ok', 1, 1),
            ('mineru', 'ok', None, 1), ('mineru', 'ok', 0, 1), ('mineru', 'ok', 1000, 0),
        ]):
            conn.execute("INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,"
                         "created_at,elapsed_ms,quality_json) VALUES (?,?,?,'test',?,?,'now',?,?)",
                         (str(i), artifact['id'], parser, str(i), status, elapsed, json.dumps({'page_count': pages})))
    detail = api.paper_detail(conn, paths, artifact['paper_id'], None)
    assert detail['mineru_seconds_per_page'] == (45 if history else 20)
    assert detail['mineru_estimate_minutes'] == (3 if history else 1)


@pytest.mark.parametrize('busy', [True, False])
def test_parser_stops_only_idle_managed_server(paths: Paths, corpus: Path,
                                             monkeypatch: pytest.MonkeyPatch, busy: bool) -> None:
    stopped: list[bool] = []
    monkeypatch.setattr(mineru.LlamaCppServer, 'is_idle', lambda *a: not busy)
    monkeypatch.setattr(mineru.LlamaCppServer, 'stop', lambda *a, **kw: stopped.append(True))
    monkeypatch.setattr(mineru, 'find_command', lambda *a: '/fake/mineru')
    def command(*args: Any, **kwargs: Any) -> Any:
        assert stopped == ([] if busy else [True])
        raise RuntimeError('reached parser')
    monkeypatch.setattr(mineru, '_run_command', command)
    config = Config()
    config.models.servers['llamacpp'] = LlamaCppServerConfig(managed=True)
    with pytest.raises(RuntimeError, match='reached parser'):
        mineru.MinerUParser(paths, config).parse(corpus / 'single_column.pdf', paths.root / 'out', config.parsing)
