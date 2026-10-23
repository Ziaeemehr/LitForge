"""Which library the desktop window opens, and the per-user desktop settings file."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from litforge.desktop import workspace as ws


def _lib(path: Path) -> Path:
    (path / "data").mkdir(parents=True)
    return path


@pytest.fixture()
def dirs(tmp_path: Path) -> dict[str, Path]:
    return {name: tmp_path / name for name in ("install", "cwd", "home", "config")}


def _resolve(dirs: dict[str, Path], explicit: Path | None = None) -> Path:
    return ws.resolve_workspace(explicit, settings=ws.load_settings(dirs["config"]), install_dir=dirs["install"],
                                cwd=dirs["cwd"], home=dirs["home"])


def test_explicit_path_wins(dirs: dict[str, Path], tmp_path: Path) -> None:
    _lib(dirs["install"])
    chosen = tmp_path / "elsewhere"
    assert _resolve(dirs, chosen) == chosen.resolve()


def test_saved_workspace_comes_before_the_install_folder(dirs: dict[str, Path], tmp_path: Path) -> None:
    _lib(dirs["install"])
    saved = _lib(tmp_path / "saved")
    ws.save_settings(dirs["config"], {"workspace": str(saved)})
    assert _resolve(dirs) == saved.resolve()


def test_a_saved_workspace_that_disappeared_is_skipped(dirs: dict[str, Path], tmp_path: Path) -> None:
    _lib(dirs["install"])
    ws.save_settings(dirs["config"], {"workspace": str(tmp_path / "gone")})
    assert _resolve(dirs) == dirs["install"].resolve()


def test_existing_install_library_is_recognised(dirs: dict[str, Path]) -> None:
    _lib(dirs["install"])
    _lib(dirs["cwd"])
    assert _resolve(dirs) == dirs["install"].resolve()


def test_source_launch_directory_with_a_library(dirs: dict[str, Path]) -> None:
    _lib(dirs["cwd"])
    assert _resolve(dirs) == dirs["cwd"].resolve()


def test_new_install_default_is_litforge_in_home(dirs: dict[str, Path]) -> None:
    assert _resolve(dirs) == (dirs["home"] / "LitForge").resolve()


def test_spaces_and_persian_characters_round_trip(dirs: dict[str, Path], tmp_path: Path) -> None:
    lib = _lib(tmp_path / "کتابخانه من" / "My Papers")
    ws.save_settings(dirs["config"], {"workspace": str(lib), "port": 8811})
    raw = (dirs["config"] / "desktop.json").read_text(encoding="utf-8")
    assert "کتابخانه من" in raw  # readable, not \u escapes
    assert _resolve(dirs) == lib.resolve()
    assert ws.load_settings(dirs["config"])["port"] == 8811


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as a normal user")
def test_a_read_only_workspace_is_refused_with_a_clear_message(dirs: dict[str, Path], tmp_path: Path) -> None:
    locked = _lib(tmp_path / "locked")
    (locked / "data").chmod(0o500)
    try:
        with pytest.raises(ws.WorkspaceError, match="cannot write"):
            _resolve(dirs, locked)
    finally:
        (locked / "data").chmod(0o700)


def test_a_damaged_settings_file_is_ignored(dirs: dict[str, Path]) -> None:
    dirs["config"].mkdir(parents=True)
    (dirs["config"] / "desktop.json").write_text("{not json", encoding="utf-8")
    assert ws.load_settings(dirs["config"]) == {}


def test_save_merges_and_writes_atomically(dirs: dict[str, Path]) -> None:
    ws.save_settings(dirs["config"], {"workspace": "/a"})
    ws.save_settings(dirs["config"], {"port": 9001})
    assert json.loads((dirs["config"] / "desktop.json").read_text(encoding="utf-8")) == {"workspace": "/a",
                                                                                         "port": 9001}
    assert [p.name for p in dirs["config"].iterdir()] == ["desktop.json"]


def test_user_config_dir_per_platform(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "APPDATA": str(tmp_path / "appdata")}
    assert ws.user_config_dir("linux", env, tmp_path) == tmp_path / "xdg" / "litforge"
    assert ws.user_config_dir("linux", {}, tmp_path) == tmp_path / ".config" / "litforge"
    assert ws.user_config_dir("win32", env, tmp_path) == tmp_path / "appdata" / "LitForge"
    assert ws.user_config_dir("darwin", env, tmp_path) == tmp_path / "Library" / "Application Support" / "LitForge"


def test_the_saved_port_is_reused_right_after_the_last_session_closed() -> None:
    """A closed server leaves its port in TIME_WAIT; the next launch must still get the same origin."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Ok(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with socket.create_connection(("127.0.0.1", port)) as c:
        c.sendall(b"GET / HTTP/1.0\r\n\r\n")
        while c.recv(100):  # read to EOF: the server closes first, so its side of the port is in TIME_WAIT
            pass
    httpd.shutdown()
    httpd.server_close()
    assert ws.preferred_port({"port": port}) == port


def test_a_port_in_use_is_not_offered() -> None:
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert ws.preferred_port({"port": taken}) != taken


def test_preferred_port_reuses_the_saved_one_when_free(dirs: dict[str, Path]) -> None:
    port = ws.preferred_port({"port": 0})
    assert 1024 < port < 65536
    assert ws.preferred_port({"port": port}) == port
