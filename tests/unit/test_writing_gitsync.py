from __future__ import annotations

import shutil
import subprocess

import pytest

from litforge.config import Paths
from litforge.projects import create_project
from litforge.storage.db import open_db
from litforge.writing import documents as wd
from litforge.writing import gitsync as gs

pytestmark = pytest.mark.skipif(not shutil.which('git'), reason='git is not installed')


def git(folder, *args):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost', *args],
                          cwd=folder, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / '.gitconfig'))
    for key in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.delenv(key, raising=False)
    paths = Paths(tmp_path / 'data', exports_root=tmp_path / 'exports')
    paths.ensure()
    conn = open_db(paths.db)
    create_project(conn, paths, 'demo')
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', '--initial-branch=main', str(remote))
    doc = wd.create_document(conn, paths, 'demo', 'Paper')
    yield conn, paths, doc, remote
    conn.close()


@pytest.mark.parametrize('url', ['https://host/a', 'http://host/a', 'ssh://git@host/a',
                                     'git@host:a/b', 'file:///tmp/repo', '/tmp/repo'])
def test_urls_allowed(url):
    assert gs.validate_url(url) == url


@pytest.mark.parametrize('url', ['-oProxyCommand=evil', 'ext::sh -c evil', 'fd::3', 'https://host/a b',
                                     'https://host/a\nb', 'ftp://host/a', 'relative/path', '', 'a\x00b'])
def test_urls_refused(url):
    with pytest.raises(wd.DocumentError):
        gs.validate_url(url)


def test_connect_and_status(env, tmp_path):
    _, _, doc, remote = env
    assert gs.status(doc) == dict(repo=False, remote=None, branch=None, changed=[], ahead=0, behind=0,
                                  merging=False, conflicts=[], last_sync=None)
    result = gs.connect(doc, str(remote))
    assert result['repo'] and result['branch'] == 'main' and result['remote'] == str(remote)
    assert result['changed'] == [] and result['last_sync'] is None
    assert '/.build/' in (doc.folder / '.git/info/exclude').read_text()
    assert '/.snapshots/' in (doc.folder / '.git/info/exclude').read_text()
    assert '.gitignore' not in git(doc.folder, 'ls-files')
    assert not (tmp_path / '.gitconfig').exists()
    head = git(doc.folder, 'rev-parse', 'HEAD')
    gs.connect(doc, remote.as_uri())
    assert git(doc.folder, 'rev-parse', 'HEAD') == head
    assert gs.disconnect(doc)['remote'] is None
    assert gs.status(doc)['repo']


def pair(env):
    conn, paths, doc, remote = env
    gs.connect(doc, str(remote))
    assert gs.sync(doc)['pushed'] == 1
    other = gs.clone(conn, paths, 'demo', 'Other', str(remote))
    return doc, other


def test_clone_and_cleanup(env):
    conn, paths, doc, remote = env
    (doc.folder / 'main.tex').rename(doc.folder / 'paper.tex')
    gs.connect(doc, str(remote))
    gs.sync(doc)
    other = gs.clone(conn, paths, 'demo', 'Other', remote.as_uri())
    assert other.template == 'git' and other.main_file == 'paper.tex'
    assert gs.status(other)['last_sync']
    root = wd.writing_root(paths, 'demo')
    before = set(root.iterdir())
    with pytest.raises(wd.DocumentError):
        gs.clone(conn, paths, 'demo', 'Failed', str(remote / 'missing'))
    assert set(root.iterdir()) == before
    (doc.folder / 'paper.tex').unlink()
    gs.sync(doc)
    with pytest.raises(wd.DocumentError, match='documentclass'):
        gs.clone(conn, paths, 'demo', 'No main', str(remote))
    assert set(root.iterdir()) == before
    assert len(wd.list_documents(conn, paths, 'demo')) == 2


def test_sync_pull_push_merge_and_snapshot(env):
    doc, other = pair(env)
    (doc.folder / 'a.tex').write_text('local')
    result = gs.sync(doc)
    assert result['pushed'] == 1 and result['pulled'] == 0
    assert gs.sync(other)['pulled'] == 1
    (doc.folder / 'a.tex').write_text('local change')
    (other.folder / 'b.tex').write_text('remote change')
    gs.sync(other)
    hook = doc.folder / '.git/hooks/post-merge'
    marker = doc.folder / 'hook-ran'
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
    hook.chmod(0o755)
    result = gs.sync(doc)
    assert result['pulled'] == 1 and result['pushed'] == 2 and not result['conflicts']
    assert (doc.folder / 'b.tex').read_text() == 'remote change'
    assert not marker.exists()
    assert wd.list_snapshots(doc)[0]['note'] == 'before git sync'
    assert gs.status(doc)['ahead'] == gs.status(doc)['behind'] == 0
    assert gs.status(doc)['last_sync']


def conflict(env):
    doc, other = pair(env)
    (doc.folder / 'main.tex').write_text('local\n')
    (other.folder / 'main.tex').write_text('remote\n')
    gs.sync(other)
    result = gs.sync(doc)
    assert result['conflicts'] == ['main.tex'] and result['status']['merging']
    assert gs.status(doc)['ahead'] == gs.status(doc)['behind'] == 1
    return doc


def test_conflict_finish(env):
    doc = conflict(env)
    with pytest.raises(wd.DocumentError, match='markers'):
        gs.sync(doc)
    (doc.folder / 'main.tex').write_text('resolved\n')
    result = gs.sync(doc)
    assert result['pushed'] == 2 and result['conflicts'] == [] and not result['status']['merging']


def test_abort(env):
    doc = conflict(env)
    assert not gs.abort_merge(doc)['merging']
    assert (doc.folder / 'main.tex').read_text() == 'local\n'


def test_missing_git(env, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    with pytest.raises(wd.DocumentError, match='git is not installed'):
        gs.connect(env[2], str(env[3]))


def test_noninteractive_and_timeout(env, monkeypatch):
    doc = env[2]
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    gs.connect(doc, str(env[3]))
    gs.sync(doc)
    for args, kw in calls:
        assert 'core.hooksPath=/dev/null' in args and 'protocol.ext.allow=never' in args
        assert kw['env']['GIT_TERMINAL_PROMPT'] == '0' and kw['env']['GIT_ASKPASS'] == ''
        assert kw['env']['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'
        assert kw['timeout'] == (120 if {'fetch', 'push', 'set-head'} & set(args) else 30)


def test_template_hooks_never_run(env, tmp_path, monkeypatch):
    conn, paths, doc, remote = env
    template = tmp_path / 'template'
    hooks = template / 'hooks'
    hooks.mkdir(parents=True)
    marker = tmp_path / 'hook-ran'
    for name in ('post-checkout', 'post-merge', 'pre-commit', 'pre-push'):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
        hook.chmod(0o755)
    monkeypatch.setenv('GIT_TEMPLATE_DIR', str(template))
    gs.connect(doc, str(remote))
    gs.sync(doc)
    other = gs.clone(conn, paths, 'demo', 'Other', str(remote))
    (other.folder / 'new.tex').write_text('new')
    gs.sync(other)
    gs.sync(doc)
    assert not marker.exists()


def test_git_errors(env, monkeypatch):
    doc = env[2]
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 1, '', 'fatal: Authentication failed\nlast meaningful line\n\n'))
    with pytest.raises(wd.DocumentError, match='last meaningful line; git could not sign in'):
        gs._git(doc, 'fetch', 'origin', network=True)


def test_existing_repo(env):
    doc, remote = env[2:]
    git(doc.folder, 'init', '--initial-branch=paper')
    git(doc.folder, 'add', '.')
    git(doc.folder, 'commit', '-m', 'existing')
    head = git(doc.folder, 'rev-parse', 'HEAD')
    assert gs.connect(doc, str(remote))['branch'] == 'paper'
    assert git(doc.folder, 'rev-parse', 'HEAD') == head


def test_remote_default_branch_and_offline_status(env):
    doc, other = pair(env)
    git(other.folder, 'checkout', '-b', 'editor')
    (other.folder / 'new.tex').write_text('new')
    assert gs.status(other)['changed'] == ['new.tex']
    (doc.folder / 'remote.tex').write_text('remote')
    gs.sync(doc)
    assert gs.status(other)['behind'] == 0  # status does not fetch
    result = gs.sync(other)
    assert result['pulled'] == 1 and result['pushed'] == 2
    assert git(doc.folder, 'ls-remote', '--heads', 'origin', 'editor') == ''
    assert gs.status(other)['branch'] == 'editor'


# ---- round 1 review: database, lock, credentials, Overleaf's master branch -------------------------------


def test_clone_does_not_hold_the_database_while_it_downloads(env, monkeypatch):
    conn, paths, doc, remote = env
    gs.connect(doc, str(remote))
    gs.sync(doc)
    seen = {}
    real = gs._git

    def watch(target, *args, **kw):
        if args and args[0] == 'clone':
            other = open_db(paths.db)  # another connection must still be able to write
            other.execute("UPDATE writing_documents SET name=name WHERE id=?", (doc.id,))
            other.commit()
            other.close()
            seen['clone'] = True
        return real(target, *args, **kw)

    monkeypatch.setattr(gs, '_git', watch)
    copy = gs.clone(conn, paths, 'demo', 'Copy', str(remote))
    assert seen['clone'] and (copy.folder / 'main.tex').is_file() and (copy.folder / '.git').is_dir()
    assert not [p for p in copy.folder.parent.iterdir() if p.name.startswith('.clone-')]


def test_saving_is_not_blocked_while_git_talks_to_the_remote(env, monkeypatch):
    import threading

    conn, paths, doc, remote = env
    gs.connect(doc, str(remote))
    real = gs._git
    saved = {}

    def slow(target, *args, **kw):
        if args and args[0] in ('fetch', 'push'):
            done = threading.Event()

            def save():
                with wd._lock(doc.id):
                    saved[args[0]] = True
                done.set()

            threading.Thread(target=save).start()
            assert done.wait(5), 'the document lock was held during ' + args[0]
        return real(target, *args, **kw)

    monkeypatch.setattr(gs, '_git', slow)
    gs.sync(doc)
    assert saved == {'fetch': True, 'push': True}


def test_credentials_in_urls_are_masked(env):
    _, _, doc, remote = env
    gs.connect(doc, 'https://ann:secret-token@example.com/repo.git')
    assert gs.status(doc)['remote'] == 'https://***@example.com/repo.git'
    assert 'secret-token' not in gs.mask('fatal: unable to access https://ann:secret-token@example.com/repo.git/')


def test_the_users_own_ssh_command_is_kept(env, monkeypatch):
    monkeypatch.setenv('GIT_SSH_COMMAND', 'ssh -i ~/.ssh/work_key')
    assert gs._env()['GIT_SSH_COMMAND'] == 'ssh -i ~/.ssh/work_key -o BatchMode=yes'
    monkeypatch.delenv('GIT_SSH_COMMAND')
    assert gs._env()['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'


def test_connecting_to_a_remote_with_another_project_says_to_clone_it(env, tmp_path):
    # An Overleaf project already has its own history (on master); joining it means cloning it.
    conn, paths, doc, _ = env
    remote = tmp_path / 'overleaf.git'
    git(tmp_path, 'init', '--bare', '--initial-branch=master', str(remote))
    seed = tmp_path / 'seed'
    git(tmp_path, 'clone', str(remote), str(seed))
    (seed / 'main.tex').write_text('\\documentclass{article}\n')
    git(seed, 'add', '-A')
    git(seed, 'commit', '-m', 'seed')
    git(seed, 'push', 'origin', 'HEAD:master')
    gs.connect(doc, str(remote))
    with pytest.raises(wd.DocumentError, match='clone it instead'):
        gs.sync(doc)
    assert 'refs/heads/main' not in git(remote, 'for-each-ref')
    assert gs.status(doc)['merging'] is False


# ---- round 3 (Codex review): repository config that runs programs, conflicts in any file, secrets ---------


@pytest.mark.parametrize('key,value', [('filter.pwn.clean', 'touch pwned; cat'), ('merge.x.driver', 'touch pwned'),
                                       ('diff.x.textconv', 'touch pwned'), ('core.sshCommand', 'touch pwned'),
                                       ('include.path', '/tmp/other.config'),
                                       ('remote.origin.uploadpack', 'touch pwned'),
                                       ('core.pager', 'touch pwned'), ('credential.helper', '!touch pwned')])
def test_repository_config_that_runs_programs_is_refused(env, key, value):
    _, _, doc, remote = env
    gs.connect(doc, str(remote))
    git(doc.folder, 'config', '--local', key, value)
    (doc.folder / '.gitattributes').write_text('*.tex filter=pwn merge=x diff=x\n')
    with pytest.raises(wd.DocumentError, match='runs programs'):
        gs.sync(doc)
    assert not (doc.folder / 'pwned').exists()


def conflict_in(env, name, mine, theirs):
    doc, other = pair(env)
    (doc.folder / name).write_bytes(b'base\n')
    gs.sync(doc)
    gs.sync(other)
    (other.folder / name).write_bytes(theirs)
    gs.sync(other)
    (doc.folder / name).write_bytes(mine)
    return doc, gs.sync(doc)


def test_unresolved_markers_in_any_text_file_block_the_sync(env):
    doc, result = conflict_in(env, 'package.sty', b'mine\n', b'theirs\n')
    assert result['conflicts'] == ['package.sty']
    with pytest.raises(wd.DocumentError, match='package.sty'):
        gs.sync(doc)


def test_a_binary_conflict_needs_a_choice(env):
    doc, result = conflict_in(env, 'figures/plot.png', b'\x89PNG\0mine', b'\x89PNG\0theirs')
    assert result['conflicts'] == ['figures/plot.png']
    with pytest.raises(wd.DocumentError, match='plot.png'):
        gs.sync(doc)


def test_secrets_never_appear(env, monkeypatch):
    assert gs.mask('https://secret-token@example.com/r.git') == 'https://***@example.com/r.git'
    assert gs.mask('https://ann:pw@example.com/r.git') == 'https://***@example.com/r.git'

    def stall(*a, **k):
        raise subprocess.TimeoutExpired(['git', 'clone', 'https://ann:secret@example.com/r.git'], 120)

    monkeypatch.setattr(gs.subprocess, 'run', stall)
    with pytest.raises(wd.DocumentError) as info:
        gs._git(env[2], 'fetch', network=True)
    assert 'secret' not in str(info.value) and 'timed out' in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
