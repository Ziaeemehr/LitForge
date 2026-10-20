"""Noninteractive git remotes for document folders."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from litforge.config import Paths
from litforge.storage.db import now
from litforge.writing import documents as wd
from litforge.writing.documents import Document, DocumentError

_CONFIG = [item for key in ('core.hooksPath=/dev/null', 'protocol.ext.allow=never', 'protocol.fd.allow=never',
                            'core.symlinks=false', 'core.fsmonitor=false', 'credential.interactive=never')
           for item in ('-c', key)]


def validate_url(url: str) -> str:
    if (not url or url.startswith('-') or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or url.lower().startswith(('ext::', 'fd::'))):
        raise DocumentError('invalid git remote URL')
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise DocumentError('invalid git remote URL') from exc
    if url.startswith('/') or (parsed.scheme in {'http', 'https', 'ssh', 'file'}
                               and (parsed.netloc or parsed.scheme == 'file')):
        return url
    if re.fullmatch(r'[^/@:]+@[^/:]+:.+', url):
        return url
    raise DocumentError('invalid git remote URL')


def mask(text: str) -> str:
    """A token in a remote URL (https://user:token@host/...) is never shown."""
    return re.sub(r'(://)[^/@\s]+@', r'\1***@', text)


def _env() -> dict[str, str]:
    # Never prompt: a missing credential fails at once. The user's own SSH command (a key per host) is kept.
    ssh = (os.environ.get('GIT_SSH_COMMAND') or 'ssh').strip() + ' -o BatchMode=yes'
    return {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '', 'SSH_ASKPASS': '', 'GIT_SSH_COMMAND': ssh,
            'GIT_MERGE_AUTOEDIT': 'no'}


def _git(doc: Document | Path, *args: str, network: bool = False,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    git = shutil.which('git')
    if not git:
        raise DocumentError('git is not installed')
    folder = doc.folder if isinstance(doc, Document) else doc
    try:
        result = subprocess.run([git, *_CONFIG, *args], cwd=folder, env=_env(), capture_output=True,
                                text=True, errors='replace', timeout=120 if network else 30)
    except subprocess.TimeoutExpired:  # its text holds the command line, URL (and token) included
        raise DocumentError(f'git timed out after {120 if network else 30} s') from None
    except OSError as exc:
        raise DocumentError(mask(f'git failed: {exc}')) from None
    if check and result.returncode:
        raise DocumentError(_error(result))
    return result


def _error(result: subprocess.CompletedProcess[str]) -> str:
    lines = [s.strip() for s in result.stderr.splitlines() if s.strip()]
    message = mask(lines[-1] if lines else 'git operation failed')
    if re.search(r'auth|permission denied|could not read|credential|publickey', result.stderr, re.I):
        message += '; git could not sign in: set up a credential helper or an SSH key for this host'
    if 'unrelated histories' in result.stderr:
        message = 'the remote already holds a different project; clone it instead (Write → Clone from git)'
    return message


def _out(doc: Document, *args: str) -> str:
    return _git(doc, *args, check=False).stdout.strip()


# Repository settings that make git run a program. LitForge's own -c options and environment cover hooks, fsmonitor
# and SSH; a repository the user connects may carry others (a cloned one never does: git writes its config).
_RUNS = re.compile(r'^(filter\.|merge\..+\.driver$|diff\..+\.(textconv|command)$|include\.|includeif\.|'
                   r'core\.(sshcommand|askpass|editor|pager|gitproxy|hookspath|fsmonitor)$|sequence\.editor$|'
                   r'credential\.|remote\..+\.(uploadpack|receivepack|proxy)$|gpg\.|pager\.|alias\.|'
                   r'http\..*proxy|protocol\.)', re.I)


def _check_config(doc: Document) -> None:
    keys = _out(doc, 'config', '--local', '--name-only', '--list').splitlines()
    risky = sorted({k for k in keys if _RUNS.match(k.strip())})
    if risky:
        raise DocumentError('this repository has settings that runs programs (' + ', '.join(risky[:4]) +
                            '); LitForge does not sync it. Remove them with git config --local --unset, or sync it '
                            'yourself')


def _repo(doc: Document) -> bool:
    return (doc.folder / '.git').is_dir() and _out(doc, 'rev-parse', '--is-inside-work-tree') == 'true'


def _exclude(doc: Document) -> None:
    path = doc.folder / '.git/info/exclude'
    path.parent.mkdir(exist_ok=True)
    text = path.read_text() if path.exists() else ''
    missing = [p for p in ('/.build/', '/.snapshots/') if p not in text.splitlines()]
    if missing:
        path.write_text(text + '\n' + '\n'.join(missing) + '\n')


def _stamp(doc: Document) -> None:
    (doc.folder / '.git/litforge-last-sync').write_text(now())


def _identity(doc: Document) -> list[str]:
    config = []
    for key, fallback in [('user.name', 'LitForge'), ('user.email', 'litforge@localhost')]:
        if not _out(doc, 'config', '--get', key):
            config.extend(['-c', f'{key}={fallback}'])
    return config


def _commit(doc: Document, message: str) -> None:
    _git(doc, *_identity(doc), 'commit', '--no-gpg-sign', '-m', message)


def _remote_branch(doc: Document, branch: str) -> str | None:
    if _git(doc, 'show-ref', '--verify', '--quiet', f'refs/remotes/origin/{branch}', check=False).returncode == 0:
        return branch
    head = _out(doc, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
    return head.removeprefix('origin/') if head.startswith('origin/') else None


def status(doc: Document) -> dict[str, Any]:
    with wd._lock(doc.id):
        result: dict[str, Any] = dict(repo=False, remote=None, branch=None, changed=[], ahead=0, behind=0,
                                      merging=False, conflicts=[], last_sync=None)
        if not _repo(doc):
            return result
        branch = _out(doc, 'symbolic-ref', '--short', 'HEAD') or None
        result.update(repo=True, remote=mask(_out(doc, 'remote', 'get-url', 'origin')) or None, branch=branch,
                      merging=(doc.folder / '.git/MERGE_HEAD').exists())
        entries = _git(doc, 'status', '--porcelain=v1', '-z').stdout.split('\0')
        changed = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            if entry:
                changed.append(entry[3:])
                if 'R' in entry[:2] or 'C' in entry[:2]:
                    i += 1
            i += 1
        result['changed'] = changed
        result['conflicts'] = _git(doc, 'diff', '--name-only', '--diff-filter=U', '-z').stdout.strip('\0').split('\0')
        result['conflicts'] = [p for p in result['conflicts'] if p]
        remote = _remote_branch(doc, branch) if branch else None
        if remote:
            counts = _out(doc, 'rev-list', '--left-right', '--count', f'HEAD...refs/remotes/origin/{remote}').split()
            if len(counts) == 2:
                result.update(ahead=int(counts[0]), behind=int(counts[1]))
        stamp = doc.folder / '.git/litforge-last-sync'
        result['last_sync'] = stamp.read_text().strip() if stamp.exists() else None
        return result


def connect(doc: Document, url: str) -> dict[str, Any]:
    validate_url(url)
    with wd._lock(doc.id):
        if not _repo(doc):
            _git(doc, 'init', '--initial-branch=main', '--', '.')
        action = 'set-url' if _out(doc, 'remote', 'get-url', 'origin') else 'add'
        _git(doc, 'remote', action, '--', 'origin', url)
        _exclude(doc)
        if not _out(doc, 'rev-parse', '--verify', 'HEAD'):
            _git(doc, 'add', '-A', '--', '.')
            _commit(doc, 'LitForge: initial')
        return status(doc)


def clone(conn: sqlite3.Connection, paths: Paths, project: str, name: str, url: str) -> Document:
    validate_url(url)
    root = wd.writing_root(paths, project)
    root.mkdir(parents=True, exist_ok=True)
    # The download happens before the document is created: creating it holds the database for writing.
    staging = Path(tempfile.mkdtemp(prefix='.clone-', dir=root))
    try:
        _git(staging, 'clone', '--', url, '.', network=True)

        def populate(doc: Document) -> None:
            for child in staging.iterdir():
                os.replace(child, doc.folder / child.name)
            candidates = wd.main_candidates(doc)
            if not candidates:
                raise DocumentError(r'no .tex file with \documentclass')
            wd.set_settings(conn, paths, doc.id, main_file=candidates[0])
            _exclude(doc)
            _stamp(doc)

        return wd._create_document(conn, paths, project, name, 'git', populate)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _commit_all(doc: Document, message: str) -> None:
    _git(doc, 'add', '-A', '--', '.')
    if _git(doc, 'diff', '--cached', '--quiet', check=False).returncode:
        _commit(doc, message)


def _check_resolved(doc: Document, conflicts: list[str]) -> None:
    """Every file git could not merge must be settled before the merge is committed: no markers left in a text file
    (whatever its type: .sty and .cls too), and a binary file, which has no markers, cannot be judged at all."""
    for name in conflicts:
        path = wd.resolve_inside(doc.folder, name, allow_hidden=True)
        data = path.read_bytes() if path.is_file() else b''
        if b'\0' in data[:8000]:
            raise DocumentError(f'{name} changed on both sides and is not text, so there is nothing to compare: '
                                f'Abort merge, agree on one version of it, and sync again')
        if re.search(rb'^(<<<<<<< |=======\r?$|>>>>>>> )', data, re.M):
            raise DocumentError(f'resolve the conflict markers in {name} before syncing')


def sync(doc: Document) -> dict[str, Any]:
    """Commit, fetch, merge, push. The document's lock is held only for the local steps: saves must not wait while
    git talks to the remote (up to two minutes)."""
    with wd._lock(doc.id):
        before = status(doc)
        if not before['remote']:
            raise DocumentError('document is not connected to a git remote')
        for url in [_out(doc, 'remote', 'get-url', 'origin'),
                    *_out(doc, 'remote', 'get-url', '--push', '--all', 'origin').splitlines()]:
            validate_url(url)
        branch = before['branch']
        if not branch:
            raise DocumentError('check out a branch before syncing')
        _check_config(doc)
        _exclude(doc)
        finishing = before['merging']
        if finishing:
            _check_resolved(doc, before['conflicts'])
            _git(doc, 'add', '-A', '--', '.')
            _commit(doc, 'LitForge: resolve merge')
        else:
            wd.create_snapshot(doc, 'before git sync')
            _commit_all(doc, f'LitForge: {now()}')
    pulled = 0
    if not finishing:
        _git(doc, 'fetch', '--', 'origin', network=True)
        _git(doc, 'remote', 'set-head', 'origin', '--auto', network=True, check=False)  # the remote's default branch
        with wd._lock(doc.id):
            _commit_all(doc, f'LitForge: {now()}')  # saved while fetching
            remote = _remote_branch(doc, branch)
            if remote:
                ref = f'refs/remotes/origin/{remote}'
                pulled = int(_out(doc, 'rev-list', '--count', f'HEAD..{ref}') or 0)
                result = _git(doc, *_identity(doc), 'merge', '--no-edit', '--no-gpg-sign', '--', ref, check=False)
                if result.returncode:
                    current = status(doc)
                    if current['conflicts']:
                        return dict(pulled=0, pushed=0, conflicts=current['conflicts'], status=current)
                    raise DocumentError(_error(result))
    remote = _remote_branch(doc, branch)
    target = remote or branch
    pushed = int(_out(doc, 'rev-list', '--count', f'refs/remotes/origin/{remote}..HEAD') or 0) if remote else int(
        _out(doc, 'rev-list', '--count', 'HEAD') or 0)
    _git(doc, 'push', *(['-u'] if not remote else []), '--', 'origin', f'HEAD:refs/heads/{target}', network=True)
    with wd._lock(doc.id):
        _stamp(doc)
        return dict(pulled=pulled, pushed=pushed, conflicts=[], status=status(doc))


def abort_merge(doc: Document) -> dict[str, Any]:
    with wd._lock(doc.id):
        _git(doc, 'merge', '--abort')
        return status(doc)


def disconnect(doc: Document) -> dict[str, Any]:
    with wd._lock(doc.id):
        _git(doc, 'remote', 'remove', '--', 'origin')
        return status(doc)
