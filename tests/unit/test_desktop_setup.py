"""Choosing the window engine at install time, without sudo and without a compiler."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from litforge.desktop import setup
from litforge.desktop.workspace import load_settings


class Runner:
    """Records commands; answers like the real tools would on the machine being described."""

    def __init__(self, *, gtk: str | None = "4.1", fail: str = "", deb_lib: bool = True) -> None:
        self.gtk, self.fail, self.deb_lib = gtk, fail, deb_lib
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        text = " ".join(cmd)
        if self.fail and self.fail in text:
            return subprocess.CompletedProcess(cmd, 1, "", "network is unreachable")
        if "import gi" in text:
            return subprocess.CompletedProcess(cmd, 0 if self.gtk else 1, (self.gtk or "") + "\n", "")
        if cmd[:2] == ["dpkg-deb", "-x"] and self.deb_lib:
            lib = Path(cmd[3]) / "usr" / "lib" / "x86_64-linux-gnu" / "libxcb-cursor.so.0"
            lib.parent.mkdir(parents=True, exist_ok=True)
            lib.write_bytes(b"\x7fELF")
        if cmd[:2] == ["apt-get", "download"]:
            (Path(kw["cwd"]) / "libxcb-cursor0_0.1.4_amd64.deb").write_bytes(b"deb")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _setup(tmp_path: Path, runner: Runner, *, platform: str = "linux", env: dict[str, str] | None = None,
           tools: tuple[str, ...] = ("apt-get", "dpkg-deb"), lib_present: bool = False,
           engine: str = "auto") -> setup.DesktopSetup:
    return setup.setup_desktop(tmp_path / "install", tmp_path / "config", platform=platform,
                               env=env if env is not None else {"DISPLAY": ":0"}, run=runner,
                               which=lambda name: f"/usr/bin/{name}" if name in tools else None,
                               find_library=lambda name: "libxcb-cursor.so.0" if lib_present else None,
                               uv="/usr/bin/uv", system_python="/usr/bin/python3", engine=engine)


def test_linux_with_webkitgtk_gets_a_small_host_environment(tmp_path: Path) -> None:
    runner = Runner(gtk="4.1")
    result = _setup(tmp_path, runner)
    host_env = tmp_path / "install" / "desktop-host"
    assert result.engine == "gtk"
    assert runner.commands[1] == ["/usr/bin/uv", "venv", "--allow-existing", "--python", "/usr/bin/python3",
                                  "--system-site-packages", str(host_env)]
    assert runner.commands[2][:4] == ["/usr/bin/uv", "pip", "install", "--python"]
    assert runner.commands[2][-1] == f"pywebview=={setup.PYWEBVIEW}"
    saved = load_settings(tmp_path / "config")
    assert saved["engine"] == "gtk" and saved["preload"] == []
    assert saved["host_python"] == str(host_env / "bin" / "python")
    assert not any("sudo" in c for cmd in runner.commands for c in cmd)


def test_ubuntu_2204_webkit_40_is_accepted(tmp_path: Path) -> None:
    assert _setup(tmp_path, Runner(gtk="4.0")).engine == "gtk"


def test_without_webkitgtk_qt_is_installed_and_xcb_cursor_is_fetched_without_sudo(tmp_path: Path) -> None:
    runner = Runner(gtk=None)
    result = _setup(tmp_path, runner)
    assert result.engine == "qt"
    assert ["/usr/bin/uv", "sync", "--inexact", "--extra", "desktop", "--extra", "desktop-qt"] in runner.commands
    assert ["apt-get", "download", "libxcb-cursor0"] in runner.commands
    libs = tmp_path / "install" / "desktop-libs"
    assert result.preload == [str(libs / "x" / "usr" / "lib" / "x86_64-linux-gnu" / "libxcb-cursor.so.0")]
    assert load_settings(tmp_path / "config")["preload"] == result.preload
    assert "host_python" not in load_settings(tmp_path / "config") or \
        load_settings(tmp_path / "config")["host_python"] is None


def test_qt_with_the_system_library_needs_no_preload(tmp_path: Path) -> None:
    runner = Runner(gtk=None)
    result = _setup(tmp_path, runner, lib_present=True)
    assert result.preload == [] and not any(cmd[0] == "apt-get" for cmd in runner.commands)


def test_qt_on_wayland_only_needs_no_xcb_library(tmp_path: Path) -> None:
    runner = Runner(gtk=None)
    result = _setup(tmp_path, runner, env={"WAYLAND_DISPLAY": "wayland-0"})
    assert result.preload == []


def test_qt_elsewhere_says_exactly_what_to_install(tmp_path: Path) -> None:
    runner = Runner(gtk=None)
    result = _setup(tmp_path, runner, tools=("dnf",))
    assert result.preload == []
    assert "sudo dnf install xcb-util-cursor" in result.message


def test_windows_and_macos_use_the_system_engine(tmp_path: Path) -> None:
    assert _setup(tmp_path, Runner(), platform="win32").engine == "edgechromium"
    assert _setup(tmp_path, Runner(), platform="darwin").engine == "cocoa"


def test_an_explicit_engine_wins(tmp_path: Path) -> None:
    runner = Runner(gtk="4.1")
    assert _setup(tmp_path, runner, engine="qt", lib_present=True).engine == "qt"
    assert not any("import gi" in " ".join(cmd) for cmd in runner.commands)


def test_a_failed_step_reports_its_output(tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError, match="network is unreachable"):
        _setup(tmp_path, Runner(gtk="4.1", fail="pip install"))


def test_detect_gtk_without_a_system_python(tmp_path: Path) -> None:
    runner = Runner()
    assert setup.detect_gtk(str(tmp_path / "missing-python"), run=runner) is None
    assert runner.commands == []


def test_the_first_launch_on_linux_sets_the_engine_up_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import litforge.desktop as desktop

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setattr(desktop.instance, "activate_existing", lambda ws: "none")
    calls: list[str] = []

    def fake_setup(*a: Any, **kw: Any) -> setup.DesktopSetup:
        calls.append(kw["engine"])
        from litforge.desktop.workspace import save_settings
        save_settings(a[1], {"engine": "qt", "host_python": None, "preload": []})
        return setup.DesktopSetup("qt")

    opened: list[str] = []
    monkeypatch.setattr(desktop, "setup_desktop", fake_setup)
    monkeypatch.setattr(desktop.host, "run_desktop", lambda backend, **kw: opened.append(kw["gui"]) or 0)
    assert desktop.launch_desktop(tmp_path / "ws") == 0
    assert desktop.launch_desktop(tmp_path / "ws") == 0
    assert calls == ["auto"] and opened == ["qt", "qt"]
