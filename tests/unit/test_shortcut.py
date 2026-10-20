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
