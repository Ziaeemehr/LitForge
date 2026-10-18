from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from litforge.analysis.validation import locate_quote
from litforge.cli import app
from litforge.config import Config, Paths
from litforge.ingest import ingest_paths
from litforge.parsing import mineru_parser as mineru
from litforge.parsing import run

FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'mineru_middle.json'


def test_mapping() -> None:
    doc = mineru.map_middle_json(json.loads(FIXTURE.read_text()), '4.0.12')
    assert doc.parser == 'mineru' and doc.parser_version == '4.0.12'
    assert doc.page_count == 2
    assert [s.kind for s in doc.sections] == ['abstract', 'body', 'references', 'appendix']
    assert doc.sections[1].level == 2
    text = doc.sections[0].paragraphs[0]
    assert text.bbox == (60, 160, 540, 240)
    assert 'from $2^{10}$ up to $2^{17}$ samples' in text.text
    assert 'ordinary digits 1 0 stay spaced' in text.text
    assert doc.sections[0].paragraphs[1].text == r'$$x = 2^{10} \tag{1}$$'
    assert doc.furniture == ['Synthetic Journal', '1', 'Synthetic footer']
    assert 'Synthetic Journal' not in doc.full_text()
    table = doc.assets[0]
    assert table.kind == 'table' and table.caption == 'Table 1: Scores'
    assert table.data and '<table>' in str(table.data['html'])
    assert '| Method | Score |' in str(table.data['markdown'])
    assert 'Method | Score\nTest & control | 0.95' in doc.full_text()
    assert doc.assets[1].kind == 'figure'
    assert 'Figure 1: Synthetic chart' in doc.full_text()
    for page in doc.pages:
        assert page.char_count == sum(len(p.text) for s in doc.sections for p in s.paragraphs if p.page == page.index)
    match = locate_quote('from 2^{10} up to 2^{17} samples', text.text)
    assert match is not None
    assert text.text[slice(*match)] == 'from $2^{10}$ up to $2^{17}$ samples'


@pytest.fixture()
def executable(tmp_path: Path) -> Path:
    script = tmp_path / 'fake-mineru'
    script.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ['version']:
    print('4.0.12'); sys.exit(0)
record = Path(os.environ['RECORD'])
with record.open('a') as f: f.write(json.dumps({{'argv': sys.argv, 'env': dict(os.environ)}})+'\\n')
mode = os.environ.get('FAKE_MODE')
if mode == 'fail' or mode == 'oom' and os.environ.get('MINERU_DEVICE_MODE') != 'cpu':
    print('OutOfMemory synthetic GPU failure' if mode == 'oom' else 'synthetic failure tail', file=sys.stderr)
    sys.exit(1)
Path(sys.argv[sys.argv.index('-o')+1]).write_text({FIXTURE.read_text()!r})
''')
    script.chmod(0o755)
    return script


@pytest.mark.parametrize('mode', ['ok', 'oom', 'fail'])
def test_runner(executable: Path, paths: Paths, corpus: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    record = paths.root / 'record'
    monkeypatch.setenv('RECORD', str(record))
    monkeypatch.setenv('FAKE_MODE', mode)
    monkeypatch.setenv('MINERU_API_KEY', 'secret')
    monkeypatch.setenv('MINERU_VL_API_KEY', 'secret')
    config = Config()
    config.parsing.mineru.command = str(executable)
    parser = mineru.MinerUParser(paths, config)
    if mode == 'fail':
        with pytest.raises(RuntimeError, match='synthetic failure tail'):
            parser.parse(corpus / 'single_column.pdf', paths.root / 'out', config.parsing)
    else:
        assert parser.parse(corpus / 'single_column.pdf', paths.root / 'out', config.parsing).parser == 'mineru'
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    assert len(calls) == (2 if mode == 'oom' else 1)
    for call in calls:
        assert call['argv'] == [str(executable), 'parse', str(corpus / 'single_column.pdf'),
                                '-o', str(paths.root / 'out' / 'middle.json'), '-f', 'middle_json']
        env = call['env']
        assert env['CI'] == '1' and env['MINERU_HOME'] == str(paths.root / 'models' / 'mineru')
        assert 'MINERU_API_KEY' not in env and 'MINERU_VL_API_KEY' not in env
    if mode == 'oom':
        assert calls[1]['env']['MINERU_DEVICE_MODE'] == 'cpu'
        assert parser.reasons == ['mineru: retried on CPU after GPU out of memory']


def test_detection_order(paths: Paths, monkeypatch: pytest.MonkeyPatch, executable: Path) -> None:
    config = Config()
    which = mineru.shutil.which
    monkeypatch.setattr(mineru.shutil, 'which', lambda cmd: str(executable) if cmd == 'mineru-kit' else which(cmd))
    assert mineru.find_command(paths, config.parsing) == str(executable)
    managed = paths.root / 'tools/mineru/bin/mineru-kit'
    managed.parent.mkdir(parents=True)
    managed.symlink_to(executable)
    assert mineru.find_command(paths, config.parsing) == str(managed)
    config.parsing.mineru.command = str(executable)
    assert mineru.find_command(paths, config.parsing) == str(executable)


@pytest.mark.parametrize(('fallback', 'installed', 'docling', 'expected'), [
    ('auto', True, True, 'mineru'), ('auto', False, True, 'docling'),
    ('auto', False, False, 'pymupdf'), ('mineru', False, True, 'pymupdf'),
    ('mineru', True, True, 'mineru'), ('docling', True, True, 'docling'), ('none', True, True, 'pymupdf'),
])
def test_routes(conn: sqlite3.Connection, paths: Paths, corpus: Path, monkeypatch: pytest.MonkeyPatch,
                fallback: str, installed: bool, docling: bool, expected: str) -> None:
    config = Config()
    config.parsing.complex_fallback = fallback
    config.parsing.router.max_table_count = 0
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    artifact = run.artifacts_for_project(conn, None)[0]
    monkeypatch.setattr(run, 'find_command', lambda *a: '/fake' if installed else None)
    monkeypatch.setattr(run, 'docling_available', lambda: docling)
    class Fake:
        version = 'test'
        reasons: list[str] = []
        def __init__(self, *a: Any):
            pass
        def parse(self, *a: Any) -> Any:
            return mineru.map_middle_json(json.loads(FIXTURE.read_text()), self.version)
    class M(Fake):
        name = 'mineru'
    class D(Fake):
        name = 'docling'
    monkeypatch.setattr(run, 'MinerUParser', M)
    monkeypatch.setattr(run, 'DoclingParser', D)
    assert run.parse_artifact(conn, paths, config, artifact).parser == expected


def test_failed_forced_run_preserves_current(conn: sqlite3.Connection, paths: Paths, corpus: Path,
                                            executable: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config()
    config.parsing.mineru.command = str(executable)
    monkeypatch.setenv('RECORD', str(paths.root / 'record'))
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    artifact = run.artifacts_for_project(conn, None)[0]
    first = run.parse_artifact(conn, paths, config, artifact, parser='mineru', force=True)
    assert first.outcome == 'parsed'
    from litforge.analysis.ledger import get_spans, record_spans
    from litforge.indexing.fts import search
    spans = record_spans(conn, search(conn, 'budgets'))
    assert spans
    assert run.parse_artifact(conn, paths, config, artifact, parser='mineru').outcome == 'cached'
    current = conn.execute('SELECT id,output_dir FROM parse_runs WHERE is_current=1').fetchone()
    assert conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] > 0
    monkeypatch.setenv('FAKE_MODE', 'fail')
    failed = run.parse_artifact(conn, paths, config, artifact, parser='mineru', force=True)
    assert failed.outcome == 'failed'
    assert conn.execute('SELECT id FROM parse_runs WHERE is_current=1').fetchone()[0] == current['id']
    assert (paths.root / current['output_dir'] / 'canonical.json').is_file()
    assert conn.execute("SELECT COUNT(*) FROM parse_runs WHERE status='failed' AND is_current=0").fetchone()[0] == 1
    monkeypatch.delenv('FAKE_MODE')
    run.parse_artifact(conn, paths, config, artifact, parser='mineru', force=True)
    assert conn.execute('SELECT COUNT(*) FROM parse_runs').fetchone()[0] == 3
    assert get_spans(conn, [s.id for s in spans]) == spans
    assert conn.execute('SELECT COUNT(*) FROM parse_runs WHERE id=?', (spans[0].parse_run_id,)).fetchone()[0] == 1


def test_install_preview_no_confirmation(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mineru, 'install_mineru', lambda *a, **kw: pytest.fail('must not install'))
    result = CliRunner().invoke(app, ['--data-dir', str(paths.root), 'parse', 'install-mineru'], input='n\n')
    assert 'GB' in result.output
    assert not (paths.root / 'tools').exists()


@pytest.mark.parametrize(('seconds', 'expected'), [(120, 600), (300, 900)])
def test_timeout_and_owned_server(paths: Paths, corpus: Path, executable: Path,
                                  monkeypatch: pytest.MonkeyPatch, seconds: float, expected: float) -> None:
    import subprocess

    from litforge.config import LlamaCppServerConfig
    config = Config()
    config.parsing.mineru.command = str(executable)
    config.parsing.mineru.seconds_per_page_timeout = seconds
    config.models.servers['llamacpp'] = LlamaCppServerConfig(managed=True)
    stopped: list[bool] = []
    monkeypatch.setattr(mineru.LlamaCppServer, 'stop', lambda *a, **kw: stopped.append(True))
    monkeypatch.setattr(mineru.LlamaCppServer, 'is_idle', lambda *a: True)
    parser = mineru.MinerUParser(paths, config)
    def timeout(argv: list[str], **kwargs: Any) -> Any:
        assert stopped == [True]
        assert kwargs['timeout'] == expected
        raise subprocess.TimeoutExpired(argv, 900, stderr=b'synthetic timeout tail')
    monkeypatch.setattr(mineru, '_run_command', timeout)
    with pytest.raises(RuntimeError, match='synthetic timeout tail'):
        parser.parse(corpus / 'single_column.pdf', paths.root / 'out', config.parsing)


def test_ui_jobs(paths: Paths, corpus: Path, executable: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.ui.server import App, Job
    config = Config()
    config.parsing.mineru.command = str(executable)
    monkeypatch.setenv('RECORD', str(paths.root / 'record'))
    ui = App(paths, config)
    conn = ui.db()
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    paper = run.artifacts_for_project(conn, None)[0]['paper_id']
    conn.close()
    with pytest.raises(ValueError, match='Confirm'):
        ui.mineru_install_job({})
    jobs: list[tuple[str, Any]] = []
    def start(kind: str, project: str, work: Any) -> Job:
        jobs.append((kind, work))
        return Job(id='test', kind=kind, project=project)
    monkeypatch.setattr(ui, 'start_job', start)
    job = ui.mineru_parse_job(paper)
    assert job.kind == 'mineru-parse' and len(jobs) == 1
    jobs[0][1](job)
    assert job.report and job.report['outcome'] == 'parsed'
    assert any('about 1 min' in message for message in job.messages)
    install = ui.mineru_install_job({'confirmed': True})
    assert install.kind == 'mineru-install'
    assert len(jobs) == 2


def test_empty_output_rejected() -> None:
    data = json.loads(FIXTURE.read_text())
    for page in data['pages']:
        page['blocks'] = []
    with pytest.raises(ValueError, match='no body text'):
        mineru.map_middle_json(data, '4.0.12')


def test_cli_selects_papers_and_parser(conn: sqlite3.Connection, paths: Paths, corpus: Path,
                                      executable: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml
    monkeypatch.setenv('RECORD', str(paths.root / 'record'))
    ingest_paths(conn, paths, [corpus / 'single_column.pdf', corpus / 'two_column.pdf'], None)
    papers = [a['paper_id'] for a in run.artifacts_for_project(conn, None)]
    (paths.root / 'config.yaml').write_text(yaml.safe_dump({'parsing': {'mineru': {'command': str(executable)}}}))
    args = ['--data-dir', str(paths.root), 'parse', '--parser', 'mineru', '--paper', papers[0], '--json']
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1 and data[0]['paper_id'] == papers[0] and data[0]['parser'] == 'mineru'
    assert conn.execute('SELECT COUNT(*) FROM parse_runs').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] > 0
    repeated = CliRunner().invoke(app, [*args[:-1], '--paper', papers[1], '--json'])
    assert repeated.exit_code == 0, repeated.output
    assert len(json.loads(repeated.output)) == 2
    unknown = CliRunner().invoke(app, [*args[:-1], '--paper', 'no-such-paper', '--dry-run'])
    assert unknown.exit_code == 0 and 'skipped 1 paper(s)' in unknown.stderr and 'no-such-paper' in unknown.stderr


def test_legacy_no_docling_disables_all_fallbacks(conn: sqlite3.Connection, paths: Paths, corpus: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config()
    config.parsing.router.max_table_count = 0
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    artifact = run.artifacts_for_project(conn, None)[0]
    monkeypatch.setattr(run, 'find_command', lambda *a: pytest.fail('fallback detection must not run'))
    assert run.parse_artifact(conn, paths, config, artifact, use_docling=False).parser == 'pymupdf'
    assert run.parse_artifact(conn, paths, config, artifact, parser='pymupdf').parser == 'pymupdf'


def test_api_routes_without_sockets(paths: Paths, corpus: Path, executable: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from litforge.ui.server import App, Job, make_handler
    config = Config()
    config.parsing.mineru.command = str(executable)
    ui = App(paths, config)
    def request(path: str, body: dict[str, Any] | None = None, origin: bool = True) -> tuple[int, Any]:
        handler = object.__new__(make_handler(ui))
        handler.path = path
        handler.headers = {}  # type: ignore[assignment]
        handler.wfile = io.BytesIO()
        output: list[Any] = []
        handler._json = lambda value, status=200: output.extend([status, value])  # type: ignore[method-assign]
        handler._error = lambda status, value: output.extend([status, value])  # type: ignore[method-assign]
        handler._same_origin = lambda: origin  # type: ignore[method-assign]
        handler._body = lambda: body or {}  # type: ignore[method-assign]
        handler.do_GET() if body is None else handler.do_POST()
        return output[0], output[1]
    code, status = request('/api/mineru')
    assert code == 200 and status['installed'] and status['version'] == '4.0.12'
    assert status['models_bytes'] == 0 and 'GB' in status['notice']
    assert request('/api/mineru/install', {})[0] == 400
    assert request('/api/mineru/install', {'confirmed': True}, origin=False)[0] == 403
    monkeypatch.setattr(ui, 'start_job', lambda kind, project, work: Job(id='fake', kind=kind, project=project))
    assert request('/api/mineru/install', {'confirmed': True})[1]['kind'] == 'mineru-install'
    conn = ui.db()
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    paper = run.artifacts_for_project(conn, None)[0]['paper_id']
    conn.close()
    assert request(f'/api/papers/{paper}/parse-mineru', {})[1]['kind'] == 'mineru-parse'
    detail = request(f'/api/papers/{paper}')[1]
    assert detail['has_pdf'] and detail['page_count'] == 3 and detail['pdf_url']
    assert request('/api/papers/missing/parse-mineru', {})[0] == 404


@pytest.mark.parametrize('uv', [True, False])
def test_install_isolated_and_pinned(paths: Paths, monkeypatch: pytest.MonkeyPatch, uv: bool) -> None:
    import subprocess
    calls: list[list[str]] = []
    monkeypatch.setattr(mineru.shutil, 'which', lambda cmd: '/fake/uv' if uv else None)
    def command(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert 0 < kw['timeout'] <= 2700
        return subprocess.CompletedProcess(argv, 0, '', 'Downloading dependencies\nInstalled\n')
    monkeypatch.setattr(mineru, '_run_command', command)
    messages: list[str] = []
    mineru.install_mineru(paths, messages.append)
    assert len(calls) == 2
    assert str(paths.root / 'tools' / 'mineru') in calls[0]
    assert calls[1][-1] == 'mineru[core]==4.0.12'
    assert str(paths.root / 'tools' / 'mineru' / 'bin' / 'python') in calls[1]
    assert any('Downloading' in m for m in messages)


def test_oom_reason_persisted(conn: sqlite3.Connection, paths: Paths, corpus: Path,
                              executable: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config()
    config.parsing.mineru.command = str(executable)
    monkeypatch.setenv('RECORD', str(paths.root / 'record'))
    monkeypatch.setenv('FAKE_MODE', 'oom')
    ingest_paths(conn, paths, [corpus / 'single_column.pdf'], None)
    artifact = run.artifacts_for_project(conn, None)[0]
    run.parse_artifact(conn, paths, config, artifact, parser='mineru')
    row = conn.execute('SELECT route_reasons FROM parse_runs WHERE is_current=1').fetchone()
    assert 'mineru: retried on CPU after GPU out of memory' in json.loads(row[0])


def test_install_yes(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Path] = []
    monkeypatch.setattr(mineru, 'install_mineru', lambda paths, progress, **kw: called.append(paths.root))
    result = CliRunner().invoke(app, ['--data-dir', str(paths.root), 'parse', 'install-mineru', '--yes'])
    assert result.exit_code == 0 and 'GB' in result.output
    assert called == [paths.root]


def test_version_cached_by_command(executable: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    real_run = mineru._run_command
    def version(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return real_run(argv, **kwargs)
    monkeypatch.setattr(mineru, '_run_command', version)
    mineru.command_version.cache_clear()
    assert mineru.command_version(str(executable)) == '4.0.12'
    assert mineru.command_version(str(executable)) == '4.0.12'
    assert calls == [[str(executable), 'version']]
