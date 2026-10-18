from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from unit.test_models_panel import handler_request

from litforge import tools, updates
from litforge.cli import app as cli
from litforge.config import Config, Paths, load_settings, save_mailto
from litforge.ui.server import App, make_handler


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()


def commit(root: Path, subject: str) -> None:
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
        'commit', '--allow-empty', '-m', subject)


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    origin, clone, writer = (tmp_path / name for name in ('origin.git', 'clone', 'writer'))
    subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(origin)], check=True, capture_output=True)
    subprocess.run(['git', 'clone', str(origin), str(writer)], check=True, capture_output=True)
    (writer / 'pyproject.toml').write_text('[project]\nname = "litforge"\n')
    git(writer, 'add', '.')
    commit(writer, 'Initial')
    git(writer, 'push', 'origin', 'main')
    subprocess.run(['git', 'clone', str(origin), str(clone)], check=True, capture_output=True)
    package = clone / 'src/litforge/__init__.py'
    monkeypatch.setattr(tools, '_package_file', lambda: package)
    return clone, writer


@pytest.mark.parametrize('remote,local,expected', [(0, 0, 'up_to_date'), (1, 0, 'available'),
                                                 (0, 1, 'up_to_date'), (1, 1, 'diverged')])
def test_git_states(checkout: tuple[Path, Path], paths: Paths, remote: int, local: int, expected: str) -> None:
    clone, writer = checkout
    if remote:
        commit(writer, '<remote subject>')
        git(writer, 'push', 'origin', 'main')
    if local:
        commit(clone, 'Local commit')
    before = git(clone, 'rev-parse', 'HEAD')
    git(clone, 'branch', 'untouched')
    git(clone, 'tag', 'untouched')
    report = updates.check_for_update(paths, force=True)
    assert report['status'] == expected
    assert (report['behind'], report['ahead']) == (remote, local)
    assert report['branch'] == 'main' and report['local_changes'] is False
    assert report['latest']['subject'] == ('<remote subject>' if remote else 'Initial')
    datetime.fromisoformat(report['latest']['date'])
    datetime.fromisoformat(report['checked_at'])
    assert git(clone, 'rev-parse', 'HEAD') == before
    assert git(clone, 'rev-parse', 'untouched') == before
    assert git(clone, 'rev-parse', 'refs/tags/untouched') == before
    assert not git(clone, 'status', '--porcelain')
    assert json.loads((paths.root / 'state/update-check.json').read_text()) == report


@pytest.mark.parametrize('branch,dirty', [('main', True), ('feature', False), ('feature', True)])
def test_installer_refusal(checkout: tuple[Path, Path], paths: Paths, branch: str, dirty: bool) -> None:
    clone, writer = checkout
    commit(writer, 'Remote change')  # an update exists, so the refusal matters
    git(writer, 'push', 'origin', 'main')
    if branch != 'main':
        git(clone, 'checkout', '-b', branch)
    if dirty:
        (clone / 'untracked.txt').write_text('keep me')
    result = updates.check_for_update(paths, force=True)
    assert result['branch'] == branch and result['local_changes'] is dirty
    assert 'installer will refuse' in result['message']
    assert result['command'].startswith('git -C ')
    assert 'pull --ff-only origin main' in result['command'] and 'curl' not in result['command']
    assert git(clone, 'branch', '--show-current') == branch
    if dirty:
        assert (clone / 'untracked.txt').read_text() == 'keep me'


@pytest.mark.parametrize('branch,dirty', [('main', True), ('feature', False)])
def test_up_to_date_checkout_only_says_so(checkout: tuple[Path, Path], paths: Paths, branch: str,
                                          dirty: bool) -> None:
    clone, _ = checkout
    if branch != 'main':
        git(clone, 'checkout', '-b', branch)
    if dirty:
        (clone / 'untracked.txt').write_text('keep me')
    result = updates.check_for_update(paths, force=True)
    assert result['status'] == 'up_to_date' and result['message'] == 'LitForge is up to date.'


@pytest.mark.parametrize('system,fragment', [('Darwin', 'curl -LsSf'), ('Linux', 'curl -LsSf'),
                                          ('Windows', 'powershell -ExecutionPolicy ByPass')])
def test_platform_commands(checkout: tuple[Path, Path], paths: Paths, monkeypatch: pytest.MonkeyPatch,
                           system: str, fragment: str) -> None:
    monkeypatch.setattr(updates.platform, 'system', lambda: system)
    result = updates.check_for_update(paths, force=True)
    assert result['command'].startswith(fragment)
    assert result['command'] in Path('README.md').read_text()


def test_not_git_and_installed_package(paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / 'zip'
    root.mkdir()
    (root / 'pyproject.toml').write_text('[project]\nname = "litforge"\n')
    monkeypatch.setattr(tools, '_package_file', lambda: root / 'src/litforge/__init__.py')
    assert updates.check_for_update(paths, force=True)['status'] == 'not_git'
    monkeypatch.setattr(tools, '_package_file', lambda: tmp_path / 'site-packages/litforge/__init__.py')
    result = updates.check_for_update(paths, force=True)
    assert result['status'] == 'not_git' and 'installer' in result['message']


def test_offline_and_cache(checkout: tuple[Path, Path], paths: Paths) -> None:
    clone, writer = checkout
    first = updates.check_for_update(paths)
    git(clone, 'remote', 'set-url', 'origin', str(writer / 'missing'))
    assert updates.check_for_update(paths) == first
    error = updates.check_for_update(paths, force=True)
    assert error['status'] == 'error' and len(error['message']) < 200
    assert updates.check_for_update(paths) == error  # failures do not retry on every page load


def test_stale_cache(checkout: tuple[Path, Path], paths: Paths) -> None:
    updates.check_for_update(paths)
    cache = paths.root / 'state/update-check.json'
    data = json.loads(cache.read_text())
    data['checked_at'] = (datetime.now(UTC) - timedelta(days=1, seconds=1)).isoformat()
    cache.write_text(json.dumps(data))
    commit(checkout[1], 'New day')
    git(checkout[1], 'push', 'origin', 'main')
    assert updates.check_for_update(paths)['status'] == 'available'
    cache.write_text('{broken')
    assert updates.check_for_update(paths)['status'] == 'available'


def test_incomplete_cache_and_unwritable_cache(checkout: tuple[Path, Path], paths: Paths,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    state = paths.root / 'state'
    state.mkdir()
    cache = state / 'update-check.json'
    cache.write_text(json.dumps({'status': 'up_to_date', 'checked_at': datetime.now(UTC).isoformat()}))
    assert updates.check_for_update(paths)['branch'] == 'main'
    def read_only(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError('read only')
    monkeypatch.setattr(updates.tempfile, 'NamedTemporaryFile', read_only)
    assert updates.check_for_update(paths, force=True)['status'] == 'up_to_date'


def test_missing_git(checkout: tuple[Path, Path], paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError('git')
    monkeypatch.setattr(updates.subprocess, 'run', missing)
    assert updates.check_for_update(paths, force=True)['status'] == 'error'


def test_timeout_and_argv(checkout: tuple[Path, Path], paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.run
    calls = []
    def timeout(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv[3:])
        assert isinstance(argv, list) and not kwargs.get('shell')
        assert 0 < kwargs['timeout'] <= 10
        if argv[3] == 'fetch':
            assert argv[3:] == ['fetch', '--quiet', '--no-tags', 'origin', 'main']
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        return original(argv, **kwargs)
    monkeypatch.setattr(updates.subprocess, 'run', timeout)
    assert updates.check_for_update(paths, force=True)['status'] == 'error'
    assert calls[-1][0] == 'fetch'


@pytest.mark.parametrize('source', ['setting', 'env'])
def test_disabled_and_force(checkout: tuple[Path, Path], paths: Paths, monkeypatch: pytest.MonkeyPatch,
                            source: str) -> None:
    updates.check_for_update(paths, force=True)
    if source == 'setting':
        paths.settings.write_text('{"update_check": false}')
    else:
        monkeypatch.setenv('LITFORGE_NO_UPDATE_CHECK', '1')
    original = updates.subprocess.run
    monkeypatch.setattr(updates.subprocess, 'run', lambda *a, **kw: pytest.fail('automatic git when disabled'))
    assert updates.check_for_update(paths)['status'] == 'disabled'
    monkeypatch.setattr(updates.subprocess, 'run', original)
    assert updates.check_for_update(paths, force=True)['status'] == 'up_to_date'


def test_update_api_background_and_settings(checkout: tuple[Path, Path], paths: Paths,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(paths, Config())
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = updates.check_for_update
    calls = []
    def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        entered.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(updates, 'check_for_update', slow)
    try:
        start = time.monotonic()
        code, report = handler_request(app, '/api/update')
        assert time.monotonic() - start < .05
        assert code == 200 and report['status'] == 'checking'
        assert entered.wait(1)
        assert handler_request(app, '/api/update')[1]['status'] == 'checking'
        assert len(calls) == 1
    finally:
        release.set()
    assert finished.wait(5)
    assert handler_request(app, '/api/update')[1]['status'] == 'up_to_date'
    commit(checkout[1], 'Forced API update')
    git(checkout[1], 'push', 'origin', 'main')
    assert handler_request(app, '/api/update/check', {})[1]['status'] == 'available'
    save_mailto(paths, 'user@example.org')
    assert handler_request(app, '/api/settings', {'update_check': False})[0] == 200
    assert load_settings(paths) == {'mailto': 'user@example.org', 'update_check': False}
    assert handler_request(app, '/api/settings')[1]['update_check'] is False
    assert handler_request(app, '/api/update')[1]['status'] == 'disabled'
    assert handler_request(app, '/api/update/check', {})[1]['status'] == 'available'
    assert handler_request(app, '/api/settings', {'update_check': 'false'})[0] == 400


def test_api_cached_result_on_restart(checkout: tuple[Path, Path], paths: Paths,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    cached = updates.check_for_update(paths, force=True)
    monkeypatch.setattr(updates, 'check_for_update', lambda *a, **kw: pytest.fail('fresh cached API fetched'))
    info = handler_request(App(paths, Config()), '/api/update')[1]
    assert {key: info[key] for key in cached} == cached


@pytest.mark.parametrize('source', ['setting', 'env'])
def test_api_disabled_never_starts_worker(checkout: tuple[Path, Path], paths: Paths,
                                         monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    updates.check_for_update(paths, force=True)
    if source == 'setting':
        paths.settings.write_text('{"update_check": false}')
    else:
        monkeypatch.setenv('LITFORGE_NO_UPDATE_CHECK', '1')
    monkeypatch.setattr(updates, 'check_for_update', lambda *a, **kw: pytest.fail('disabled API started a worker'))
    info = handler_request(App(paths, Config()), '/api/update')[1]
    assert info['status'] == 'disabled' and info['update_check_enabled'] is False


def test_zip_inside_unrelated_checkout(checkout: tuple[Path, Path], paths: Paths,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    zip_root = checkout[0] / 'zip'
    zip_root.mkdir()
    (zip_root / 'pyproject.toml').write_text('[project]\nname = "litforge"\n')
    monkeypatch.setattr(tools, '_package_file', lambda: zip_root / 'src/litforge/__init__.py')
    monkeypatch.setattr(updates.subprocess, 'run', lambda *a, **kw: pytest.fail('checked enclosing repository'))
    assert updates.check_for_update(paths, force=True)['status'] == 'not_git'


def test_fetch_leaves_other_refs_alone(checkout: tuple[Path, Path], paths: Paths) -> None:
    clone, writer = checkout
    git(writer, 'checkout', '-b', 'other')
    commit(writer, 'Other branch')
    git(writer, 'push', 'origin', 'other')
    git(clone, 'fetch', 'origin')
    refs = git(clone, 'for-each-ref', '--format=%(refname) %(objectname)')
    symbolic_head = git(clone, 'symbolic-ref', 'refs/remotes/origin/HEAD')
    commit(writer, 'Other branch changed')
    git(writer, 'tag', 'remote-tag')
    git(writer, 'push', 'origin', 'other', '--tags')
    git(writer, 'checkout', 'main')
    commit(writer, 'Main changed')
    git(writer, 'push', 'origin', 'main')
    assert updates.check_for_update(paths, force=True)['status'] == 'available'
    after = git(clone, 'for-each-ref', '--format=%(refname) %(objectname)')
    def without_main(value: str) -> list[str]:
        # origin/HEAD still points to origin/main, so its resolved object follows main.
        return [line for line in value.splitlines()
                if not line.startswith(('refs/remotes/origin/main ', 'refs/remotes/origin/HEAD '))]
    assert without_main(refs) == without_main(after)
    assert git(clone, 'symbolic-ref', 'refs/remotes/origin/HEAD') == symbolic_head


def test_check_preserves_index_and_working_tree(checkout: tuple[Path, Path], paths: Paths) -> None:
    clone, _ = checkout
    project = clone / 'pyproject.toml'
    project.write_text(project.read_text() + '# staged change\n')
    git(clone, 'add', 'pyproject.toml')
    project.write_text(project.read_text() + '# unstaged change\n')
    staged, unstaged = git(clone, 'diff', '--cached'), git(clone, 'diff')
    assert updates.check_for_update(paths, force=True)['local_changes'] is True
    assert git(clone, 'diff', '--cached') == staged
    assert git(clone, 'diff') == unstaged


def test_update_post_origin_guard(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(paths, Config())
    handler = object.__new__(make_handler(app))
    handler.path = '/api/update/check'
    handler.headers = {'Origin': 'https://evil.example', 'Host': '127.0.0.1:8000'}  # type: ignore[assignment]
    responses = []
    handler._error = lambda code, msg: responses.append(code)  # type: ignore[method-assign]
    monkeypatch.setattr(updates, 'check_for_update', lambda *a, **kw: pytest.fail('cross-origin fetch'))
    handler.do_POST()
    assert responses == [403]


def test_cli_exit_codes(checkout: tuple[Path, Path], paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    base = ['--data-dir', str(paths.root), 'update']
    monkeypatch.setenv('LITFORGE_NO_UPDATE_CHECK', '1')
    assert runner.invoke(cli, base + ['--check']).exit_code == 0
    commit(checkout[1], 'New release')
    git(checkout[1], 'push', 'origin', 'main')
    result = runner.invoke(cli, base + ['--check'])
    assert result.exit_code == 10, (result.output, result.exception)
    assert 'New release' in result.output and 'curl' in result.output
    assert runner.invoke(cli, base).exit_code == 0
    git(checkout[0], 'remote', 'set-url', 'origin', str(paths.root / 'missing'))
    assert runner.invoke(cli, base + ['--check']).exit_code == 1
