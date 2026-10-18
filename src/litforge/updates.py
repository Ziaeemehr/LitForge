"""Read-only update notices. Only an explicit terminal command can install an update."""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from litforge.config import Paths, load_settings
from litforge.tools import _install_root

CHECK_TIMEOUT_S = 10
CACHE_TTL_S = 24 * 60 * 60
_INSTALL_URL = 'https://raw.githubusercontent.com/Ziaeemehr/LitForge/main/install.'
_STATUSES = {'up_to_date', 'available', 'diverged', 'not_git', 'error'}


def automatic_enabled(paths: Paths) -> bool:
    return load_settings(paths).get('update_check', True) is not False and os.getenv('LITFORGE_NO_UPDATE_CHECK') != '1'


def _installer_command() -> str:
    if platform.system() == 'Windows':
        return f'powershell -ExecutionPolicy ByPass -c "irm {_INSTALL_URL}ps1 | iex"'
    return f'curl -LsSf {_INSTALL_URL}sh | sh'


def _cache_path(paths: Paths) -> Path:
    return paths.root / 'state' / 'update-check.json'


def _read_cache(paths: Paths) -> dict[str, Any] | None:
    try:
        value = json.loads(_cache_path(paths).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            return None
        latest = value.get('latest')
        if (value.get('status') in _STATUSES
                and all(isinstance(value.get(key), str) for key in ('checked_at', 'branch', 'command', 'message'))
                and all(type(value.get(key)) is int and value[key] >= 0 for key in ('behind', 'ahead'))
                and isinstance(value.get('local_changes'), bool) and isinstance(latest, dict)
                and all(isinstance(latest.get(key), str) for key in ('subject', 'date'))):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def _fresh(result: dict[str, Any] | None) -> bool:
    if result is None:
        return False
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(result['checked_at'])).total_seconds()
        return 0 <= age < CACHE_TTL_S
    except (ValueError, TypeError, KeyError):
        return False


def _write_cache(paths: Paths, result: dict[str, Any]) -> None:
    target = _cache_path(paths)
    temporary: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False) as stream:
            temporary = stream.name
            json.dump(result, stream, ensure_ascii=False, indent=1)
            stream.write('\n')
        os.replace(temporary, target)
    except OSError:
        pass  # A read-only data directory must not break a notice.
    finally:
        if temporary:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


def check_for_update(paths: Paths, *, force: bool = False) -> dict[str, Any]:
    """Check the imported installation, caching successes and failures for one day.

    Automatic checks respect both consent controls. A forced check is an explicit request,
    including when automatic checks are disabled. No library content is read or sent.
    """
    result: dict[str, Any] = {
        'status': 'error', 'behind': 0, 'ahead': 0, 'branch': '', 'local_changes': False,
        'latest': {'subject': '', 'date': ''}, 'checked_at': datetime.now(UTC).isoformat(),
        'command': _installer_command(), 'message': 'Could not check for updates. Try again when online.',
    }
    try:
        if not force:
            if not automatic_enabled(paths):
                return {'status': 'disabled'}
            cached = _read_cache(paths)
            if _fresh(cached):
                assert cached is not None
                return cached
        root = _install_root()
        if root is None or not (root / '.git').exists():
            result.update(status='not_git', message='Not a git install. Rerun the installer to update LitForge.')
        else:
            deadline = time.monotonic() + CHECK_TIMEOUT_S

            def git(*args: str) -> str:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('git', CHECK_TIMEOUT_S)
                return subprocess.run(
                    ['git', '-C', str(root), *args], check=True, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=remaining, stdin=subprocess.DEVNULL,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'},
                ).stdout.strip()

            result['branch'] = git('branch', '--show-current')
            result['local_changes'] = bool(git('status', '--porcelain'))
            git('fetch', '--quiet', '--no-tags', 'origin', 'main')
            behind = int(git('rev-list', '--count', 'HEAD..FETCH_HEAD'))
            ahead = int(git('rev-list', '--count', 'FETCH_HEAD..HEAD'))
            subject, date = git('log', '-1', '--format=%s%x00%cI', 'FETCH_HEAD').split('\x00', 1)
            status = 'diverged' if behind and ahead else 'available' if behind else 'up_to_date'
            result.update(status=status, behind=behind, ahead=ahead, latest={'subject': subject, 'date': date})
            day = date[:10]  # the commit date is enough for people; the full timestamp stays in `latest`
            message = {
                'up_to_date': 'LitForge is up to date.' + (f' ({ahead} local commits are not on GitHub.)'
                                                           if ahead else ''),
                'available': f'An update is available: {behind} new changes — {subject} ({day}).',
                'diverged': 'Local and remote history have diverged. Review local commits before updating.',
            }[status]
            if status != 'up_to_date' and (result['branch'] != 'main' or result['local_changes']):
                # The installer only fast-forwards a clean main checkout; show the git command instead.
                quoted = ("'" + str(root).replace("'", "''") + "'" if platform.system() == 'Windows'
                          else shlex.quote(str(root)))
                result['command'] = f'git -C {quoted} pull --ff-only origin main'
                message += (' The installer will refuse this checkout (non-main branch or local changes): '
                            'commit or stash your changes first, then run the git command below.')
            result['message'] = message
    except Exception:
        # Includes missing git, offline remotes, timeouts and malformed repository/config metadata.
        # Never forward stderr, which may contain a remote URL with credentials.
        result['status'] = 'error'
        result['message'] = 'Could not check for updates. Check your connection and Git installation, then try again.'
    _write_cache(paths, result)
    return result


class UpdateChecker:
    """One shared check per server; GET only reads state and starts a daemon worker."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._lock = threading.Lock()
        self._running: threading.Event | None = None
        self._result = _read_cache(paths)

    def _info(self, result: dict[str, Any]) -> dict[str, Any]:
        return {**result, 'update_check': load_settings(self.paths).get('update_check', True) is not False,
                'update_check_enabled': automatic_enabled(self.paths)}

    def _check(self, done: threading.Event, *, force: bool) -> dict[str, Any]:
        try:
            result = check_for_update(self.paths, force=force)
            with self._lock:
                self._result = result
            return result
        finally:
            with self._lock:
                self._running = None
                done.set()

    def get(self) -> dict[str, Any]:
        if not automatic_enabled(self.paths):
            return self._info({'status': 'disabled'})
        with self._lock:
            if self._running is not None:
                return self._info({'status': 'checking'})
            if _fresh(self._result):
                assert self._result is not None
                return self._info(self._result)
            done = self._running = threading.Event()
            threading.Thread(target=self._check, args=(done,), kwargs={'force': False},
                             daemon=True, name='litforge-update-check').start()
        return self._info({'status': 'checking'})

    def force(self) -> dict[str, Any]:
        with self._lock:
            running = self._running
            if running is None:
                done = self._running = threading.Event()
        if running is not None:
            # An in-flight fetch already provides a fresh result; do not fetch twice or queue work.
            if running.wait(CHECK_TIMEOUT_S):
                with self._lock:
                    return self._info(self._result or {'status': 'error', 'message': 'Could not check for updates.'})
            return self._info({'status': 'error', 'message': 'Could not check for updates: timed out.'})
        return self._info(self._check(done, force=True))
