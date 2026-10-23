"""Choose and prepare the desktop window's engine (installer step, `litforge desktop-setup`). No sudo, no compiler.

Windows uses WebView2 and macOS WKWebView, both part of the system. On Linux the system's WebKitGTK is used when its
Python bindings are installed (every Ubuntu desktop has them): the window then runs in a small environment made from
the system Python, whose own `gi` reaches WebKitGTK. Otherwise Qt WebEngine is installed from wheels; on X11 Qt also
needs libxcb-cursor, which is fetched without sudo on Debian and Ubuntu and preloaded by the window.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from ctypes.util import find_library as ctypes_find_library
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litforge.desktop.workspace import save_settings

PYWEBVIEW = "6.2.1"
GTK_PROBE = """import gi
for v in ('4.1', '4.0'):
    try:
        gi.require_version('WebKit2', v)
        from gi.repository import WebKit2
        print(v)
        break
    except (ValueError, ImportError):
        pass
else:
    raise SystemExit(1)
"""
XCB_CURSOR_HINTS = {"dnf": "sudo dnf install xcb-util-cursor", "zypper": "sudo zypper install libxcb-cursor0",
                    "pacman": "sudo pacman -S xcb-util-cursor", "apt-get": "sudo apt install libxcb-cursor0"}

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class SetupError(Exception):
    pass


@dataclass
class DesktopSetup:
    engine: str
    host_python: str | None = None
    preload: list[str] = field(default_factory=list)
    message: str = ""


def _run(run: Run, cmd: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    result = run(list(cmd), capture_output=True, text=True, **kw)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1500:]
        raise SetupError(f"{' '.join(cmd)} failed" + (f":\n{detail}" if detail else ""))
    return result


def detect_gtk(system_python: str, *, run: Run = subprocess.run) -> str | None:
    """The WebKit2 API version ("4.1" or "4.0") the system Python can reach, or None."""
    if not os.path.exists(system_python):
        return None
    try:
        result = run([system_python, "-I", "-c", GTK_PROBE], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    version = (result.stdout or "").strip()
    return version if result.returncode == 0 and version in ("4.1", "4.0") else None


def _xcb_cursor(install_dir: Path, env: Mapping[str, str], run: Run, which: Callable[[str], str | None],
                find_library: Callable[[str], str | None]) -> tuple[list[str], str]:
    """Qt ≥ 6.5 needs libxcb-cursor on X11; most desktops have it, a plain Ubuntu does not."""
    if not env.get("DISPLAY") or find_library("xcb-cursor"):
        return [], ""
    if which("apt-get") and which("dpkg-deb"):
        libs = install_dir / "desktop-libs"
        shutil.rmtree(libs, ignore_errors=True)
        libs.mkdir(parents=True)
        _run(run, ["apt-get", "download", "libxcb-cursor0"], cwd=str(libs))
        deb = next(libs.glob("libxcb-cursor0_*.deb"), None)
        if deb is None:
            raise SetupError("apt-get download libxcb-cursor0 gave no package")
        _run(run, ["dpkg-deb", "-x", str(deb), str(libs / "x")])
        found = sorted((libs / "x").rglob("libxcb-cursor.so.0"))
        if found:
            return [str(found[0])], ""
    hint = next((cmd for tool, cmd in XCB_CURSOR_HINTS.items() if which(tool)), "install libxcb-cursor")
    return [], f"Qt needs the libxcb-cursor library on X11; install it once with: {hint}"


def setup_desktop(install_dir: Path, config_dir: Path, *, platform: str, env: Mapping[str, str],
                  run: Run = subprocess.run, which: Callable[[str], str | None] = shutil.which,
                  find_library: Callable[[str], str | None] = ctypes_find_library, uv: str = "uv",
                  system_python: str = "/usr/bin/python3", engine: str = "auto") -> DesktopSetup:
    """Prepare the engine and record it in desktop.json (engine, host_python, preload)."""
    if platform == "win32":
        result = DesktopSetup("edgechromium")
    elif platform == "darwin":
        result = DesktopSetup("cocoa")
    elif engine in ("auto", "gtk") and (engine == "gtk" or detect_gtk(system_python, run=run)):
        host_env = install_dir / "desktop-host"
        _run(run, [uv, "venv", "--allow-existing", "--python", system_python, "--system-site-packages",
                   str(host_env)])
        host_python = host_env / "bin" / "python"
        _run(run, [uv, "pip", "install", "--python", str(host_python), f"pywebview=={PYWEBVIEW}"])
        result = DesktopSetup("gtk", host_python=str(host_python), message="the system's WebKitGTK")
    else:
        _run(run, [uv, "sync", "--inexact", "--extra", "desktop", "--extra", "desktop-qt"], cwd=str(install_dir))
        preload, hint = _xcb_cursor(install_dir, env, run, which, find_library)
        result = DesktopSetup("qt", preload=preload, message=hint or "Qt WebEngine")
    save_settings(config_dir, {"engine": result.engine, "host_python": result.host_python,
                               "preload": result.preload})
    return result
