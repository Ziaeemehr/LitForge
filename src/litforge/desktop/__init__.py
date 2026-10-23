"""LitForge in its own window (pywebview): `litforge desktop` and the `litforge-app` launcher.

The window host (host.py) and the backend (`litforge desktop-server`) are separate processes. On Linux with the
system's WebKitGTK the host runs under the system Python recorded in desktop.json as `host_python`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from litforge.desktop import host, instance
from litforge.desktop.setup import DesktopSetup, SetupError, setup_desktop
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


def engine_ready() -> bool:
    """Whether `litforge desktop` can open its window here: the engine was set up (Linux) or pywebview is installed."""
    if sys.platform.startswith("linux"):
        return bool(load_settings(user_config_dir(sys.platform, os.environ, Path.home())).get("engine"))
    import importlib.util

    return importlib.util.find_spec("webview") is not None


ENGINE_LABELS = {"gtk": "the system's WebKitGTK", "qt": "Qt WebEngine", "edgechromium": "Microsoft Edge WebView2",
                 "cocoa": "the system's WebKit"}


def desktop_status() -> dict[str, object]:
    """For Settings: the engine LitForge's window uses on this computer."""
    engine = load_settings(user_config_dir(sys.platform, os.environ, Path.home())).get("engine")
    if not engine and not sys.platform.startswith("linux"):
        engine = DEFAULT_ENGINE.get(sys.platform)
    label = ENGINE_LABELS.get(str(engine), str(engine)) if engine else "not set up yet"
    return {"engine": engine, "label": label, "ready": engine_ready()}


def find_uv() -> str:
    for candidate in (shutil.which("uv"), Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise SetupError("uv was not found; run the LitForge installer again")


def configure_engine(engine: str = "auto") -> DesktopSetup:
    """Choose and prepare the window engine for this computer and remember it (desktop.json)."""
    return setup_desktop(INSTALL_DIR, user_config_dir(sys.platform, os.environ, Path.home()), platform=sys.platform,
                         env=os.environ, uv=find_uv() if sys.platform.startswith("linux") else "uv", engine=engine)


def launch_desktop(explicit: Path | None = None, gui: str | None = None) -> int:
    config_dir = user_config_dir(sys.platform, os.environ, Path.home())
    settings = load_settings(config_dir)
    if sys.platform.startswith("linux") and not gui and not settings.get("engine"):
        try:  # not set up by the installer (an older install): once, here
            setup_desktop(INSTALL_DIR, config_dir, platform=sys.platform, env=os.environ, uv=find_uv(), engine="auto")
        except SetupError as exc:
            host.show_native_error("LitForge", f"LitForge could not prepare its window:\n\n{exc}")
            return 2
        settings = load_settings(config_dir)
    try:
        workspace = resolve_workspace(explicit, settings=settings, install_dir=INSTALL_DIR, cwd=Path.cwd(),
                                      home=Path.home())
    except WorkspaceError as exc:
        host.show_native_error("LitForge", str(exc))
        return 2
    state = instance.activate_existing(workspace)  # before the port: an open window holds it
    if state == "activated":
        return 0
    if state == "busy" and not instance.wait_until_free(workspace):  # the last window is still stopping
        host.show_native_error("LitForge", "LitForge is still closing this library. Try again in a minute.")
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
               "--storage", str(storage), "--icon", str(ICON), "--workspace", str(workspace),
               *[a for p in preload for a in ("--preload", p)],
               "--", *backend]
        return subprocess.call(cmd)
    return host.run_desktop(backend, gui=engine, title="LitForge", log_path=log, icon=ICON, storage_path=storage,
                            preload=preload, workspace=workspace)


def main(argv: list[str] | None = None) -> None:
    """Entry point of the windowless `litforge-app` launcher (the LitForge icon): `--workspace PATH` opens that
    library's window. Legacy icons passing `ui` now open the desktop too; developers use `litforge ui`."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["ui"]:
        args = args[1:]  # old icons used `litforge-app ui`; retain the icon but upgrade its launch mode
    workspace = Path(args[1]) if args[:1] == ["--workspace"] and len(args) > 1 else None
    sys.exit(launch_desktop(workspace))
