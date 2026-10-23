"""The desktop host: a window that shows a loading page at once, starts the backend, then navigates to it."""

from __future__ import annotations

import ast
import importlib.abc
import importlib.util
import json
import subprocess
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from litforge.desktop import host

HOST_FILE = Path(host.__file__)
READY = "import json,sys; print(json.dumps({'url': 'http://127.0.0.1:9/', 'pid': 1}), flush=True); sys.stdin.read()"


class FakeEvent:
    def __init__(self) -> None:
        self._e = threading.Event()

    def set(self) -> None:
        self._e.set()

    def is_set(self) -> bool:
        return self._e.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._e.wait(timeout)


class FakeWindow:
    def __init__(self, html: str, close_on_navigate: bool) -> None:
        self.calls: list[tuple[str, str]] = [("html", html)]
        self.events = types.SimpleNamespace(closed=FakeEvent())
        self.close_on_navigate = close_on_navigate

    def load_url(self, url: str) -> None:
        self.calls.append(("load_url", url))
        if self.close_on_navigate:  # the user closes the window right away
            self.events.closed.set()

    def load_html(self, html: str) -> None:
        self.calls.append(("load_html", html))
        self.events.closed.set()

    def destroy(self) -> None:
        self.calls.append(("destroy", ""))
        self.events.closed.set()

    def restore(self) -> None:
        self.calls.append(("restore", ""))

    def show(self) -> None:
        self.calls.append(("show", ""))


def fake_webview(close_on_navigate: bool = True) -> tuple[types.ModuleType, dict[str, Any]]:
    seen: dict[str, Any] = {}
    mod = types.ModuleType("webview")

    def create_window(title: str, html: str = "", **kw: Any) -> FakeWindow:
        seen["window"] = FakeWindow(html, close_on_navigate)
        seen["title"] = title
        return seen["window"]

    def start(func: Any = None, args: Any = (), **kw: Any) -> None:
        seen["start"] = kw
        func(*args)

    mod.create_window = create_window  # type: ignore[attr-defined]
    mod.start = start  # type: ignore[attr-defined]
    return mod, seen


def test_window_shows_the_loading_page_first_then_navigates_to_the_backend(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, seen = fake_webview()
    monkeypatch.setitem(sys.modules, "webview", mod)
    rc = host.run_desktop([sys.executable, "-c", READY], gui="gtk", title="LitForge", log_path=tmp_path / "d.log",
                          storage_path=tmp_path / "web")
    assert rc == 0
    calls = seen["window"].calls
    assert calls[0] == ("html", host.LOADING_HTML)
    assert calls[1] == ("load_url", "http://127.0.0.1:9/")
    assert seen["start"]["gui"] == "gtk"
    assert seen["start"]["private_mode"] is False  # localStorage keeps the user's preferences
    assert seen["start"]["storage_path"] == str(tmp_path / "web")


def test_a_backend_that_fails_shows_the_error_and_the_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, seen = fake_webview()
    monkeypatch.setitem(sys.modules, "webview", mod)
    log = tmp_path / "logs" / "desktop.log"
    rc = host.run_desktop([sys.executable, "-c", "import sys; sys.stderr.write('database is locked\\n'); sys.exit(3)"],
                          gui="gtk", title="LitForge", log_path=log)
    assert rc == 1
    kind, page = seen["window"].calls[-1]
    assert kind == "load_html"
    assert "database is locked" in page and str(log) in page
    assert "database is locked" in log.read_text(encoding="utf-8")


def test_quit_in_the_page_closes_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, seen = fake_webview(close_on_navigate=False)
    monkeypatch.setitem(sys.modules, "webview", mod)
    quits = ("import json,sys,time; print(json.dumps({'url': 'http://127.0.0.1:9/', 'pid': 1}), flush=True); "
             "time.sleep(0.3)")
    rc = host.run_desktop([sys.executable, "-c", quits], gui="gtk", title="LitForge", log_path=tmp_path / "d.log")
    assert rc == 0
    assert seen["window"].calls[-1] == ("destroy", "")


def test_closing_the_window_stops_the_backend(tmp_path: Path) -> None:
    backend = host.Backend([sys.executable, "-c", READY], tmp_path / "d.log")
    assert backend.start(timeout=10) == "http://127.0.0.1:9/"
    started = time.monotonic()
    backend.stop(timeout=5)
    assert backend.returncode == 0  # it exited by itself once stdin closed
    assert time.monotonic() - started < 3


def test_a_backend_that_never_answers_is_killed(tmp_path: Path) -> None:
    backend = host.Backend([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path / "d.log")
    with pytest.raises(host.BackendError, match="did not start"):
        backend.start(timeout=0.5)
    assert backend.returncode is not None


def test_without_a_window_library_a_native_message_names_the_log(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview raises ImportError
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(host, "show_native_error", lambda title, text: shown.append((title, text)))
    rc = host.run_desktop([sys.executable, "-c", READY], gui="gtk", title="LitForge", log_path=tmp_path / "d.log")
    assert rc == 2
    assert shown and str(tmp_path / "d.log") in shown[0][1]
    assert "pywebview" in (tmp_path / "d.log").read_text(encoding="utf-8")


def test_an_engine_that_cannot_start_shows_a_native_message_and_stops_the_backend(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, _ = fake_webview()

    def broken_start(*a: Any, **kw: Any) -> None:
        raise RuntimeError("You must have either QT or GTK with Python extensions installed")

    mod.start = broken_start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", mod)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(host, "show_native_error", lambda title, text: shown.append((title, text)))
    rc = host.run_desktop([sys.executable, "-c", READY], gui="gtk", title="LitForge", log_path=tmp_path / "d.log")
    assert rc == 2
    assert "QT or GTK" in shown[0][1] and str(tmp_path / "d.log") in shown[0][1]


def test_a_second_window_for_the_same_library_brings_the_first_forward(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.desktop import instance

    mod, seen = fake_webview()
    monkeypatch.setitem(sys.modules, "webview", mod)
    ws, run_dir, marker = tmp_path / "ws", tmp_path / "run", tmp_path / "backend-started"
    activated: list[int] = []
    lease = instance.acquire_instance(ws, lambda: activated.append(1), runtime_dir=run_dir)
    try:
        rc = host.run_desktop([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], gui="gtk", title="LitForge",
                              log_path=tmp_path / "d.log", workspace=ws, runtime_dir=run_dir)
    finally:
        assert lease is not None
        lease.close()
    assert rc == 0 and activated == [1]
    assert "window" not in seen and not marker.exists()  # no second window, no second backend


def test_the_window_owns_its_library_and_comes_forward_when_asked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.desktop import instance

    mod, seen = fake_webview()
    ws, run_dir = tmp_path / "ws", tmp_path / "run"
    answered: list[bool] = []
    real_create = mod.create_window

    def create_window(title: str, html: str = "", **kw: Any) -> FakeWindow:
        window = real_create(title, html=html, **kw)
        original = window.load_url

        def load_url(url: str) -> None:  # while the window is open, a second launch asks it to come forward
            answered.append(instance.activate_existing(ws, runtime_dir=run_dir))
            original(url)

        window.load_url = load_url  # type: ignore[method-assign]
        return window

    mod.create_window = create_window  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", mod)
    rc = host.run_desktop([sys.executable, "-c", READY], gui="gtk", title="LitForge", log_path=tmp_path / "d.log",
                          workspace=ws, runtime_dir=run_dir)
    assert rc == 0 and answered == [True]
    assert ("restore", "") in seen["window"].calls and ("show", "") in seen["window"].calls
    lease = instance.acquire_instance(ws, lambda: None, runtime_dir=run_dir)  # released when the window closed
    assert lease is not None
    lease.close()


class _OnlyStdlib(importlib.abc.MetaPathFinder):
    """Fails any import that is not the standard library or webview, as under the system Python."""

    def find_spec(self, name: str, path: Any, target: Any = None) -> None:
        top = name.split(".")[0]
        if top not in sys.stdlib_module_names and top not in ("webview", "_desktop_host_under_test"):
            raise ImportError(f"host must not import {name}")
        return None


def test_host_runs_without_any_other_package(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name.split(".")[0] in ("litforge", "pydantic", "typer", "yaml"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_OnlyStdlib(), *sys.meta_path])
    spec = importlib.util.spec_from_file_location("_desktop_host_under_test", HOST_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.run_desktop)


def test_host_source_is_python_39_compatible() -> None:
    ast.parse(HOST_FILE.read_text(encoding="utf-8"), feature_version=(3, 9))


def test_cli_does_not_import_a_window_library() -> None:
    code = "import sys, litforge.cli; print('webview' in sys.modules, 'litforge.desktop.host' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False"]


def test_desktop_server_prints_its_url_and_stops_when_stdin_closes(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-m", "litforge.cli", "--data-dir", str(tmp_path / "data"),
                             "desktop-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdin is not None
        ready = json.loads(proc.stdout.readline())
        with urllib.request.urlopen(ready["url"] + "api/info", timeout=10) as resp:
            assert resp.status == 200
        assert ready["pid"] == proc.pid
        proc.stdin.close()
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
