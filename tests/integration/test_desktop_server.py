"""Owned server lifecycle used by the desktop window: start on a free port, stop once, release everything."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from litforge.config import Paths, load_config
from litforge.ui import server as srv
from litforge.ui.server import App, start_server


def _info(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url + "api/info", timeout=5) as resp:
        return json.loads(resp.read())


def _threads(prefix: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(prefix) and t.is_alive()]


@pytest.fixture()
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "data")


def test_serves_api_info_on_an_ephemeral_loopback_port(paths: Paths) -> None:
    handle = start_server(paths, load_config())
    try:
        assert handle.url.startswith("http://127.0.0.1:")
        assert not handle.url.endswith(":8765/")
        assert handle.is_running
        assert "exports_root" in _info(handle.url)
    finally:
        handle.stop()


def test_never_opens_a_browser(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url))
    handle = start_server(paths, load_config())
    handle.stop()
    time.sleep(0.6)  # serve() opens its browser after 0.5 s; start_server must not
    assert opened == []


def test_desktop_mode_has_no_idle_watchdog_and_survives_a_stale_heartbeat(paths: Paths) -> None:
    before = len(_threads("idle-watchdog"))
    handle = start_server(paths, load_config())
    try:
        handle.app.last_seen = time.monotonic() - 10 * srv.IDLE_STOP_S
        assert len(_threads("idle-watchdog")) == before
        time.sleep(0.2)
        assert handle.is_running and "exports_root" in _info(handle.url)
    finally:
        handle.stop()


def test_idle_shutdown_starts_the_watchdog(paths: Paths) -> None:
    before = len(_threads("idle-watchdog"))
    handle = start_server(paths, load_config(), idle_shutdown=True)
    try:
        assert len(_threads("idle-watchdog")) == before + 1
    finally:
        handle.stop()


def test_stop_is_idempotent_shuts_the_app_down_once_and_releases_the_port(
        paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    original = App.shutdown
    monkeypatch.setattr(App, "shutdown", lambda self, timeout=5: (calls.append(timeout), original(self, timeout)))
    handle = start_server(paths, load_config())
    port = int(handle.url.rstrip("/").rsplit(":", 1)[1])
    handle.stop()
    handle.stop()
    assert len(calls) == 1
    assert not handle.is_running
    assert _threads("litforge-http") == []
    with socket.socket() as s:  # the address is free again
        s.bind(("127.0.0.1", port))


def test_quit_from_the_page_stops_the_server(paths: Paths) -> None:
    handle = start_server(paths, load_config())
    req = urllib.request.Request(handle.url + "api/quit", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert json.loads(resp.read()) == {"stopped": True}
    handle.wait(timeout=5)
    assert not handle.is_running
    handle.stop()  # cleanup after a page Quit is still safe


def test_a_bind_failure_raises_and_leaves_no_thread(paths: Paths) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(OSError):
            start_server(paths, load_config(), port=port)
    assert _threads("litforge-http") == []


def test_stop_reports_a_timeout_instead_of_pretending(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(App, "shutdown", lambda self, timeout=5: release.wait(5))
    handle = start_server(paths, load_config())
    with pytest.raises(TimeoutError):
        handle.stop(timeout=0.2)
    release.set()
    handle.stop(timeout=5)  # the second attempt finishes the job
    assert not handle.is_running


def test_the_page_learns_it_runs_in_the_desktop_window(paths: Paths) -> None:
    desktop = start_server(paths, load_config(), desktop=True)
    browser = start_server(paths, load_config())
    try:
        assert _info(desktop.url)["desktop"] is True
        assert _info(browser.url)["desktop"] is False
    finally:
        desktop.stop()
        browser.stop()


def test_running_tasks_can_be_counted_without_stopping_anything(paths: Paths) -> None:
    from litforge.ui.server import Job

    handle = start_server(paths, load_config(), desktop=True)
    try:
        handle.app.jobs["j"] = Job(id="j", kind="search", project="p")
        with urllib.request.urlopen(handle.url + "api/running", timeout=5) as resp:
            assert json.loads(resp.read()) == {"running": 1}
        assert handle.is_running
    finally:
        handle.stop()


def test_desktop_server_announces_that_it_is_stopping(tmp_path: Path) -> None:
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-m", "litforge.cli", "--data-dir", str(tmp_path / "data"),
                             "desktop-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        url = json.loads(proc.stdout.readline())["url"]
        req = urllib.request.Request(url + "api/quit", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        assert json.loads(proc.stdout.readline()) == {"stopping": True}
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
