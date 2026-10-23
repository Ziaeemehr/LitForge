"""One LitForge window per library: the first launch owns it, later launches bring that window forward.

Ownership is an OS file lock, so a crashed window frees it at once; the record next to it (port and a random
token, readable only by this user) only says how to reach the owner. Standard library only, Python 3.9+.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO


class InstanceError(Exception):
    pass


def default_runtime_dir(platform: str, env: Mapping[str, str], home: Path) -> Path:
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "LitForge" / "run"
    if platform == "darwin":
        return home / "Library" / "Caches" / "LitForge"
    if env.get("XDG_RUNTIME_DIR"):
        return Path(env["XDG_RUNTIME_DIR"]) / "litforge"
    return home / ".cache" / "litforge" / "run"


def _paths(workspace: Path, runtime_dir: Path | None) -> tuple[Path, Path]:
    folder = runtime_dir or default_runtime_dir(sys.platform, os.environ, Path.home())
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        folder.chmod(0o700)
    key = hashlib.sha256(str(workspace.expanduser().resolve()).encode("utf-8")).hexdigest()[:24]
    return folder / f"{key}.lock", folder / f"{key}.json"


def _try_lock(f: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _write_private(path: Path, data: dict[str, object]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rec-")  # created 0600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class InstanceLease:
    """Held by the window that owns a library; close() lets the next launch own it."""

    def __init__(self, lock_file: IO[bytes], record_path: Path, server: socket.socket, token: bytes,
                 on_activate: Callable[[], object]) -> None:
        self.record_path = record_path
        self._lock_file = lock_file
        self._server = server
        self._token = token
        self._on_activate = on_activate
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="litforge-activate")
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:  # noqa: UP041 - not TimeoutError on Python 3.9 (the system Python may be 3.9)
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2)
                    ok = hmac.compare_digest(conn.recv(256).strip(), b"ACTIVATE " + self._token)
                    reply = b"NO\n"
                    if ok:  # before the reply, so the second launch exits only once the window was asked
                        shown: object = True
                        with contextlib.suppress(Exception):  # a failed focus must not stop the window
                            shown = self._on_activate()
                        reply = b"BUSY\n" if shown is False else b"OK\n"  # False: the window is closing
                    conn.sendall(reply)
                except OSError:
                    continue

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._server.close()
        self._thread.join(2)
        with contextlib.suppress(OSError):
            self.record_path.unlink()
        _unlock(self._lock_file)
        self._lock_file.close()


def activate_existing(workspace: Path, *, runtime_dir: Path | None = None, timeout: float = 2.0) -> str:
    """Ask the window that owns `workspace` to come forward: "activated", "busy" (it is closing and still
    stopping its backend) or "none" (no window, or it does not answer)."""
    _, record_path = _paths(workspace, runtime_dir)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        port, token = int(record["port"]), str(record["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return "none"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
            c.sendall(b"ACTIVATE " + token.encode("ascii") + b"\n")
            answer = c.recv(16).strip()
    except (OSError, UnicodeEncodeError):
        return "none"
    return {b"OK": "activated", b"BUSY": "busy"}.get(answer, "none")


def wait_until_free(workspace: Path, *, runtime_dir: Path | None = None, timeout: float = 60.0) -> bool:
    """Wait for a closing window to release `workspace`; True once nobody owns it."""
    lock_path, _ = _paths(workspace, runtime_dir)
    deadline = time.monotonic() + timeout
    while True:
        with open(lock_path, "a+b") as f:
            if _try_lock(f):
                _unlock(f)
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def acquire_instance(workspace: Path, on_activate: Callable[[], object], *, runtime_dir: Path | None = None,
                     wait: float = 5.0) -> InstanceLease | None:
    """Own `workspace`, or bring its window forward and return None. `on_activate` returns False once the window
    has closed (the next launch then waits). Raises InstanceError when another window holds the library but does
    not answer, or is still closing, after `wait` seconds."""
    lock_path, record_path = _paths(workspace, runtime_dir)
    deadline = time.monotonic() + wait
    while True:
        lock_file = open(lock_path, "a+b")  # noqa: SIM115 - held for the window's lifetime
        if _try_lock(lock_file):
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            server.settimeout(0.5)
            token = secrets.token_urlsafe(24)
            _write_private(record_path, {"port": server.getsockname()[1], "token": token, "pid": os.getpid()})
            return InstanceLease(lock_file, record_path, server, token.encode("ascii"), on_activate)
        lock_file.close()
        if activate_existing(workspace, runtime_dir=runtime_dir, timeout=1.0) == "activated":
            return None
        if time.monotonic() >= deadline:
            raise InstanceError("LitForge is already open for this library but does not answer. "
                                "Wait a moment and try again, or close the other LitForge window.")
        time.sleep(0.1)
