"""A launcher for the web UI that opens it without a terminal, so nobody has to remember `uv run litforge ui`."""

from __future__ import annotations

import contextlib
import os
import plistlib
import shlex
import subprocess
import tempfile
from pathlib import Path


def _desktop_quote(arg: str) -> str:
    """Quote one Exec argument as the desktop-entry spec wants (double quotes, escaped specials)."""
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'.replace("%", "%%")


def create_shortcut(platform: str, home: Path, workdir: Path, exe: Path, icon: Path | None = None,
                    gui_exe: Path | None = None) -> list[Path]:
    """Write the launcher(s) for `platform` (a sys.platform value) and return the files written.

    The data folder is resolved from the working directory, so every launcher starts in `workdir`.
    `gui_exe` is the windowless `litforge-app` (Windows); without it the Windows launcher is a .bat.
    """
    if platform == "win32":
        return [_windows(home, workdir, exe, gui_exe)]
    if platform == "darwin":
        return [_mac_app(home / "Applications" / "LitForge.app", workdir, exe, icon)]
    entry = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=LitForge",
        "Comment=Find, screen and export scientific papers",
        f"Exec={_desktop_quote(str(exe))} ui",
        f"Path={workdir}",
        "Terminal=false",  # stopped with Quit in the page
        "Categories=Education;",
    ]
    if icon is not None:
        entry.append(f"Icon={icon}")
    text = "\n".join(entry) + "\n"
    written = [_write(home / ".local" / "share" / "applications" / "litforge.desktop", text, executable=True)]
    if (home / "Desktop").is_dir():
        desktop = _write(home / "Desktop" / "litforge.desktop", text, executable=True)
        _trust(desktop)
        written.append(desktop)
    return written


def _mac_app(app: Path, workdir: Path, exe: Path, icon: Path | None = None) -> Path:
    """A minimal LitForge.app (Launchpad, Spotlight, Dock) whose executable is a shell script: no Terminal window."""
    script = f"#!/bin/sh\ncd {shlex.quote(str(workdir))} || exit 1\nexec {shlex.quote(str(exe))} ui\n"
    _write(app / "Contents" / "MacOS" / "LitForge", script, executable=True)
    info = {"CFBundleName": "LitForge", "CFBundleExecutable": "LitForge", "CFBundleIdentifier": "org.litforge.app",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0"}
    if icon is not None:
        icns = app / "Contents" / "Resources" / "LitForge.icns"
        icns.parent.mkdir(parents=True, exist_ok=True)
        if _make_icns(icon, icns):
            info["CFBundleIconFile"] = "LitForge"
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    os.utime(app)  # Finder and the Dock re-read the icon of a bundle whose date changed
    return app


def _make_icns(svg: Path, out: Path) -> bool:
    """Render `svg` into a macOS .icns with the system's own tools; False (no icon, generic one shown) if any fails."""
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / f"{svg.name}.png"  # qlmanage names its thumbnail after the source file
        iconset = Path(tmp) / "LitForge.iconset"
        iconset.mkdir()
        try:
            subprocess.run(["qlmanage", "-t", "-s", "1024", "-o", tmp, str(svg)],  # noqa: S603, S607
                           check=True, capture_output=True, timeout=60)
            for size in (16, 32, 128, 256, 512):
                for scale, suffix in ((1, ""), (2, "@2x")):
                    px = str(size * scale)
                    subprocess.run(["sips", "-z", px, px, str(png),  # noqa: S603, S607
                                    "--out", str(iconset / f"icon_{size}x{size}{suffix}.png")],
                                   check=True, capture_output=True, timeout=60)
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],  # noqa: S603, S607
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return False
    return out.is_file()


def _windows(home: Path, workdir: Path, exe: Path, gui_exe: Path | None) -> Path:
    # With OneDrive backup on, the visible desktop is the one inside OneDrive.
    desktop = home / "OneDrive" / "Desktop"
    desktop = desktop if desktop.is_dir() else home / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    if gui_exe is not None:  # a .lnk to the windowless launcher; only PowerShell (COM) can write one
        lnk = desktop / "LitForge.lnk"
        ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LF_LNK); $s.TargetPath = $env:LF_EXE; "
              "$s.Arguments = 'ui'; $s.WorkingDirectory = $env:LF_DIR; $s.Description = 'LitForge'; $s.Save()")
        env = {**os.environ, "LF_LNK": str(lnk), "LF_EXE": str(gui_exe), "LF_DIR": str(workdir)}
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],  # noqa: S603, S607
                           env=env, check=True, capture_output=True, timeout=30)
        if lnk.exists():
            return lnk
    # Fallback, with a console window. chcp 65001: read this UTF-8 file right when a path has non-ASCII letters.
    lines = ["@echo off", "chcp 65001 >nul", f'cd /d "{workdir}"', f'"{exe}" ui', "if errorlevel 1 pause", ""]
    return _write(desktop / "LitForge.bat", "\r\n".join(lines), executable=False)


def _trust(path: Path) -> None:
    """Mark a desktop icon as trusted, as "Allow Launching" does; GNOME's desktop icons only launch trusted ones."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):  # no gio (not GNOME): nothing to mark
        subprocess.run(["gio", "set", str(path), "metadata::trusted", "true"],  # noqa: S603, S607
                       check=False, capture_output=True, timeout=5)


def _write(path: Path, text: str, executable: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # bytes: keep the CRLF of .bat files on every platform
    if executable:
        path.chmod(0o755)
    return path
