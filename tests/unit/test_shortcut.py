import os
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
    assert "Terminal=true" in text
    assert "Icon=" not in text
    assert _executable(entry)


def test_linux_desktop_copy_and_icon(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    (home / "Desktop").mkdir()
    written = create_shortcut("linux", home, workdir, exe, icon=Path("/x/icon.svg"))
    assert written[1] == home / "Desktop/litforge.desktop"
    assert "Icon=/x/icon.svg" in written[1].read_text()


def test_mac_command(where: tuple[Path, Path, Path]) -> None:
    home, workdir, exe = where
    [script] = create_shortcut("darwin", home, workdir, exe)
    assert script == home / "Desktop/LitForge.command"
    text = script.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert f"cd '{workdir}'" in text
    assert f"exec '{exe}' ui" in text
    assert _executable(script)


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
