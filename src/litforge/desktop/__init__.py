"""LitForge in its own window (pywebview): `litforge desktop` and the `litforge-app` launcher.

The window host (host.py) and the backend (`litforge desktop-server`) are separate processes. On Linux with the
system's WebKitGTK the host runs under the system Python recorded in desktop.json as `host_python`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from litforge.desktop import host
from litforge.desktop.workspace import (
    WorkspaceError,
    load_settings,
    preferred_port,
    resolve_workspace,
    save_settings,
    user_config_dir,
)

INSTALL_DIR = Path(__file__).resolve().parents[3]  # the source checkout the installer created
ICON = Path(__file__).resolve().parents[1] / "ui" / "static" / "icon.svg"
DEFAULT_ENGINE = {"win32": "edgechromium", "darwin": "cocoa"}  # Linux: gtk, or qt where the installer chose it


def launch_desktop(explicit: Path | None = None, gui: str | None = None) -> int:
    config_dir = user_config_dir(sys.platform, os.environ, Path.home())
    settings = load_settings(config_dir)
    try:
        workspace = resolve_workspace(explicit, settings=settings, install_dir=INSTALL_DIR, cwd=Path.cwd(),
                                      home=Path.home())
    except WorkspaceError as exc:
        host.show_native_error("LitForge", str(exc))
        return 2
    saved_ports = settings.get("ports")
    ports: dict[str, int] = saved_ports if isinstance(saved_ports, dict) else {}
    port = preferred_port({"port": ports.get(str(workspace))})
    save_settings(config_dir, {"workspace": str(workspace), "ports": {**ports, str(workspace): port}})
    data = workspace / "data"
    log, storage = data / "logs" / "desktop.log", data / "desktop-webview"
    backend = [sys.executable, "-m", "litforge.cli", "--data-dir", str(data), "desktop-server", "--port", str(port)]
    engine = gui or settings.get("engine") or DEFAULT_ENGINE.get(sys.platform, "gtk")
    preload = [str(p) for p in settings.get("preload", []) if isinstance(p, str)]
    host_python = settings.get("host_python")
    if engine == "gtk" and isinstance(host_python, str) and Path(host_python).exists():
        cmd = [host_python, "-I", str(Path(host.__file__)), "--gui", engine, "--log", str(log),
               "--storage", str(storage), "--icon", str(ICON), *[a for p in preload for a in ("--preload", p)],
               "--", *backend]
        return subprocess.call(cmd)
    return host.run_desktop(backend, gui=engine, title="LitForge", log_path=log, icon=ICON, storage_path=storage,
                            preload=preload)


def main() -> None:
    """Entry point of the windowless `litforge-app` launcher (the LitForge icon)."""
    sys.exit(launch_desktop())
