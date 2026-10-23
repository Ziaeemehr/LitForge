"""One window per library: a second launch brings the first window forward instead of opening another."""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from litforge.desktop import instance

INSTANCE_FILE = Path(instance.__file__)


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


def _lib(path: Path) -> Path:
    (path / "data").mkdir(parents=True)
    return path


def test_the_second_launch_activates_the_first(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    first: list[int] = []
    second: list[int] = []
    lease = instance.acquire_instance(ws, lambda: first.append(1), runtime_dir=run_dir)
    try:
        assert lease is not None
        assert instance.acquire_instance(ws, lambda: second.append(1), runtime_dir=run_dir) is None
        assert first == [1] and second == []
    finally:
        assert lease is not None
        lease.close()


def test_different_libraries_are_independent(tmp_path: Path, run_dir: Path) -> None:
    a = instance.acquire_instance(_lib(tmp_path / "a"), lambda: None, runtime_dir=run_dir)
    b = instance.acquire_instance(_lib(tmp_path / "b"), lambda: None, runtime_dir=run_dir)
    try:
        assert a is not None and b is not None
    finally:
        for lease in (a, b):
            if lease:
                lease.close()


def test_the_same_library_by_another_path_is_the_same_instance(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    link = tmp_path / "link"
    link.symlink_to(ws)
    lease = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir)
    try:
        assert instance.acquire_instance(link, lambda: None, runtime_dir=run_dir) is None
    finally:
        assert lease is not None
        lease.close()


def test_close_is_idempotent_and_frees_the_library(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    lease = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir)
    assert lease is not None
    lease.close()
    lease.close()
    again = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir)
    assert again is not None
    again.close()


def test_a_stale_record_after_a_crash_does_not_block_a_new_window(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    lease = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir)
    assert lease is not None
    record = lease.record_path
    lease.close()
    record.write_text(json.dumps({"port": 9, "token": "old", "pid": 999999}), encoding="utf-8")  # left by a crash
    new = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir, wait=0.5)
    try:
        assert new is not None
        assert json.loads(record.read_text(encoding="utf-8"))["token"] != "old"
    finally:
        assert new is not None
        new.close()


def test_a_killed_owner_releases_the_library(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    code = (f"import sys,os,time; sys.path.insert(0, {str(INSTANCE_FILE.parent)!r}); import instance; "
            f"l = instance.acquire_instance(__import__('pathlib').Path({str(ws)!r}), lambda: None, "
            f"runtime_dir=__import__('pathlib').Path({str(run_dir)!r})); print('held', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        proc.kill()
        proc.wait()
        lease = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir, wait=1)
        assert lease is not None
        lease.close()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_forged_activation_is_refused(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    activated: list[int] = []
    lease = instance.acquire_instance(ws, lambda: activated.append(1), runtime_dir=run_dir)
    assert lease is not None
    try:
        port = json.loads(lease.record_path.read_text(encoding="utf-8"))["port"]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
            c.sendall(b"ACTIVATE not-the-token\n")
            assert c.recv(16).strip() == b"NO"
        assert activated == []
    finally:
        lease.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_record_and_its_folder_are_private(tmp_path: Path, run_dir: Path) -> None:
    lease = instance.acquire_instance(_lib(tmp_path / "ws"), lambda: None, runtime_dir=run_dir)
    assert lease is not None
    try:
        assert lease.record_path.stat().st_mode & 0o077 == 0
        assert run_dir.stat().st_mode & 0o077 == 0
    finally:
        lease.close()


def test_an_owner_that_does_not_answer_gives_a_clear_error(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    lease = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir)
    assert lease is not None
    try:
        lease.record_path.unlink()  # the owner is still starting: no record yet
        started = time.monotonic()
        with pytest.raises(instance.InstanceError, match="already open"):
            instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir, wait=0.4)
        assert time.monotonic() - started < 3
    finally:
        lease.close()


def test_concurrent_launches_give_exactly_one_owner(tmp_path: Path, run_dir: Path) -> None:
    ws = _lib(tmp_path / "ws")
    results: list[object] = []
    barrier = threading.Barrier(4)

    def launch() -> None:
        barrier.wait()
        results.append(instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir, wait=3))

    threads = [threading.Thread(target=launch) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    owners = [r for r in results if r is not None]
    assert len(results) == 4 and len(owners) == 1
    owners[0].close()  # type: ignore[attr-defined]


def test_activate_existing_without_a_window(tmp_path: Path, run_dir: Path) -> None:
    assert instance.activate_existing(_lib(tmp_path / "ws"), runtime_dir=run_dir) is False


def test_runtime_dir_per_platform(tmp_path: Path) -> None:
    assert instance.default_runtime_dir("linux", {"XDG_RUNTIME_DIR": str(tmp_path)}, tmp_path) == tmp_path / "litforge"
    assert instance.default_runtime_dir("linux", {}, tmp_path) == tmp_path / ".cache" / "litforge" / "run"
    assert instance.default_runtime_dir("darwin", {}, tmp_path) == tmp_path / "Library" / "Caches" / "LitForge"
    assert (instance.default_runtime_dir("win32", {"LOCALAPPDATA": str(tmp_path / "l")}, tmp_path)
            == tmp_path / "l" / "LitForge" / "run")


def test_instance_source_is_python_39_compatible_and_stdlib_only() -> None:
    tree = ast.parse(INSTANCE_FILE.read_text(encoding="utf-8"), feature_version=(3, 9))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= set(sys.stdlib_module_names)
