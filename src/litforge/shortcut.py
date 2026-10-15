"""A double-clickable launcher for the web UI, so nobody has to remember `uv run litforge ui`."""

from __future__ import annotations

import shlex
from pathlib import Path


def _desktop_quote(arg: str) -> str:
    """Quote one Exec argument as the desktop-entry spec wants (double quotes, escaped specials)."""
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'.replace("%", "%%")


def create_shortcut(platform: str, home: Path, workdir: Path, exe: Path, icon: Path | None = None) -> list[Path]:
    """Write the launcher(s) for `platform` (a sys.platform value) and return the files written.

    The data folder is resolved from the working directory, so every launcher starts in `workdir`.
    """
    if platform == "win32":
        # With OneDrive backup on, the visible desktop is the one inside OneDrive.
        desktop = home / "OneDrive" / "Desktop"
        target = (desktop if desktop.is_dir() else home / "Desktop") / "LitForge.bat"
        # chcp 65001: read this UTF-8 file correctly when a path has non-ASCII letters.
        lines = ["@echo off", "chcp 65001 >nul", f'cd /d "{workdir}"', f'"{exe}" ui', "if errorlevel 1 pause", ""]
        return [_write(target, "\r\n".join(lines), executable=False)]
    if platform == "darwin":
        target = home / "Desktop" / "LitForge.command"
        script = f"#!/bin/sh\ncd {shlex.quote(str(workdir))} || exit 1\nexec {shlex.quote(str(exe))} ui\n"
        return [_write(target, script, executable=True)]
    entry = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=LitForge",
        "Comment=Find, screen and export scientific papers",
        f"Exec={_desktop_quote(str(exe))} ui",
        f"Path={workdir}",
        "Terminal=true",
        "Categories=Education;",
    ]
    if icon is not None:
        entry.append(f"Icon={icon}")
    text = "\n".join(entry) + "\n"
    written = [_write(home / ".local" / "share" / "applications" / "litforge.desktop", text, executable=True)]
    if (home / "Desktop").is_dir():
        written.append(_write(home / "Desktop" / "litforge.desktop", text, executable=True))
    return written


def _write(path: Path, text: str, executable: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # bytes: keep the CRLF of .bat files on every platform
    if executable:
        path.chmod(0o755)
    return path
