import os
import plistlib
import sys
from pathlib import Path

import pytest

from litforge.shortcut import create_shortcut


@pytest.fixture
def where(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    return home, tmp_path / "my repo", tmp_path / "my repo" / ".venv" / "bin" / "litforge"


def _executable(path: Path) -> bool:
    return os.name == "nt" or bool(path.stat().st_mode & 0o100)


def test_linux_menu_entry(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    [entry] = create_shortcut("linux", home, workdir, exe)
    assert entry == home / ".local/share/applications/litforge.desktop"
    text = entry.read_text()
    assert f'Exec="{exe}" ui' in text
    assert f"Path={workdir}" in text
    assert "Terminal=false" in text
    assert "Icon=" not in text
    assert _executable(entry)


def test_linux_desktop_copy_and_icon(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    (home / "Desktop").mkdir()
    written = create_shortcut("linux", home, workdir, exe, icon=Path("/x/icon.svg"))
    assert written[1] == home / "Desktop/litforge.desktop"
    assert "Icon=/x/icon.svg" in written[1].read_text()


def test_mac_app_bundle(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    [app] = create_shortcut("darwin", home, workdir, exe)
    assert app == home / "Applications/LitForge.app"
    script = app / "Contents/MacOS/LitForge"
    text = script.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert f"cd '{workdir}'" in text
    assert f"exec '{exe}' ui" in text
    assert _executable(script)
    assert plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleExecutable"] == "LitForge"


def test_mac_app_icon(where: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    home, workdir, exe = where
    calls = []

    def fake_icns(svg: Path, out: Path) -> bool:
        calls.append(svg)
        out.write_bytes(b"icns")
        return True

    monkeypatch.setattr("litforge.shortcut._make_icns", fake_icns)
    [app] = create_shortcut("darwin", home, workdir, exe, icon=Path("/x/icon.svg"))
    assert calls == [Path("/x/icon.svg")]
    assert (app / "Contents/Resources/LitForge.icns").read_bytes() == b"icns"
    assert plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleIconFile"] == "LitForge"


def test_mac_app_without_icon_tools(where: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    home, workdir, exe = where
    monkeypatch.setattr("litforge.shortcut._make_icns", lambda svg, out: False)
    [app] = create_shortcut("darwin", home, workdir, exe, icon=Path("/x/icon.svg"))
    assert "CFBundleIconFile" not in plistlib.loads((app / "Contents/Info.plist").read_bytes())


@pytest.mark.skipif(sys.platform != "darwin", reason="qlmanage, sips and iconutil are macOS tools")
def test_make_icns_on_mac(tmp_path: Path) -> None:
    from litforge.shortcut import _make_icns

    svg = Path(__file__).parents[2] / "src/litforge/ui/static/icon.svg"
    out = tmp_path / "LitForge.icns"
    assert _make_icns(svg, out)
    assert out.read_bytes()[:4] == b"icns"


def test_windows_batch(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    [bat] = create_shortcut("win32", home, workdir, exe)
    assert bat == home / "Desktop/LitForge.bat"
    raw = bat.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode()
    assert f'cd /d "{workdir}"' in text
    assert f'"{exe}" ui' in text


def test_windows_onedrive_desktop(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    (home / "OneDrive" / "Desktop").mkdir(parents=True)
    [bat] = create_shortcut("win32", home, workdir, exe)
    assert bat == home / "OneDrive/Desktop/LitForge.bat"


def test_linux_desktop_mode_opens_the_window_for_this_library(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    gui = exe.with_name("litforge-app")
    [entry] = create_shortcut("linux", home, workdir, exe, gui_exe=gui, mode="desktop")
    text = entry.read_text()
    assert f'Exec="{gui}" --workspace "{workdir}"' in text
    assert "StartupWMClass=" in text  # the dock groups the window under this icon
    [entry] = create_shortcut("linux", home, workdir, exe, mode="desktop")  # no windowless launcher
    assert f'Exec="{exe}" desktop --workspace "{workdir}"' in entry.read_text()


def test_mac_desktop_mode(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    gui = exe.with_name("litforge-app")
    [app] = create_shortcut("darwin", home, workdir, exe, gui_exe=gui, mode="desktop")
    text = (app / "Contents/MacOS/LitForge").read_text()
    assert f"exec '{gui}' --workspace '{workdir}'" in text


def test_windows_desktop_mode_batch_and_link(where: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    home, workdir, exe = where
    [bat] = create_shortcut("win32", home, workdir, exe, mode="desktop")
    assert f'"{exe}" desktop --workspace "{workdir}"' in bat.read_text()
    seen: dict[str, str] = {}

    def fake_run(cmd: list[str], env: dict[str, str], **kw: object) -> None:
        seen.update(env)
        (home / "Desktop" / "LitForge.lnk").write_bytes(b"lnk")

    monkeypatch.setattr("litforge.shortcut.subprocess.run", fake_run)
    [lnk] = create_shortcut("win32", home, workdir, exe, gui_exe=exe.with_name("litforge-app.exe"), mode="desktop")
    assert lnk.name == "LitForge.lnk"
    assert seen["LF_ARGS"] == f'--workspace "{workdir}"'
    create_shortcut("win32", home, workdir, exe, gui_exe=exe.with_name("litforge-app.exe"))
    assert seen["LF_ARGS"] == "ui"  # browser mode keeps the old behaviour


def test_the_windowless_launcher_keeps_old_browser_icons_working(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    import litforge.cli
    import litforge.desktop as desktop

    called: list[object] = []
    monkeypatch.setattr(litforge.cli, "app", lambda args: called.append(("cli", args)))
    monkeypatch.setattr(desktop, "launch_desktop", lambda workspace=None, gui=None: called.append(("win", workspace))
                        or 0)
    with pytest.raises(SystemExit):
        desktop.main(["ui"])
    with pytest.raises(SystemExit):
        desktop.main(["--workspace", str(tmp_path)])
    with pytest.raises(SystemExit):
        desktop.main([])
    assert called == [("cli", ["ui"]), ("win", tmp_path), ("win", None)]


def test_shortcut_command_makes_a_desktop_icon_once_the_engine_is_ready(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    import litforge.desktop as desktop
    from litforge.cli import app

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    entry = tmp_path / ".local/share/applications/litforge.desktop"
    monkeypatch.setattr(desktop, "engine_ready", lambda: False)
    assert CliRunner().invoke(app, ["shortcut"]).exit_code == 0
    assert entry.read_text().count(" ui") == 1
    monkeypatch.setattr(desktop, "engine_ready", lambda: True)
    result = CliRunner().invoke(app, ["shortcut"])
    assert result.exit_code == 0 and "--workspace" in entry.read_text()
    assert "Closing its window stops it" in result.output
    assert CliRunner().invoke(app, ["shortcut", "--mode", "browser"]).exit_code == 0
    assert "--workspace" not in entry.read_text()


def test_shortcut_command_finds_the_windowless_launcher_next_to_litforge(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    import litforge.cli as cli
    import litforge.desktop as desktop

    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("litforge", "litforge-app"):
        (bin_dir / name).write_text("#!/bin/sh\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "litforge"), "shortcut"])
    monkeypatch.setattr("shutil.which", lambda name: None)  # not on PATH, as when run by its full path
    monkeypatch.setattr(desktop, "engine_ready", lambda: True)
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(cli.app, ["shortcut"]).exit_code == 0
    text = (tmp_path / ".local/share/applications/litforge.desktop").read_text()
    assert f'Exec="{bin_dir / "litforge-app"}" --workspace' in text
