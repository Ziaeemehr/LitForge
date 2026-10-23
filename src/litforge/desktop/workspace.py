"""Which library the desktop window opens, and the per-user desktop settings (desktop.json).

Standard library only: the window host may run under the system Python (see host.py).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SETTINGS_FILE = "desktop.json"


class WorkspaceError(Exception):
    pass


def user_config_dir(platform: str, env: Mapping[str, str], home: Path) -> Path:
    if platform == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "LitForge"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "LitForge"
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "litforge"


def load_settings(config_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((config_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(config_dir: Path, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Merge `changes` into desktop.json; written to a temporary file first, so a crash never leaves half a file."""
    data = {**load_settings(config_dir), **changes}
    config_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config_dir, prefix=".desktop-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, config_dir / SETTINGS_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return data


def _has_library(path: Path) -> bool:
    return (path / "data").is_dir()


def resolve_workspace(explicit: Path | None, *, settings: Mapping[str, Any], install_dir: Path, cwd: Path,
                      home: Path) -> Path:
    """The folder whose data/ holds the library: an explicit choice, the remembered one, the install folder's or
    the current folder's library, else ~/LitForge for a new install. Libraries are never merged or copied."""
    saved = Path(str(settings["workspace"])) if settings.get("workspace") else None
    if explicit is not None:
        chosen = explicit
    elif saved is not None and _has_library(saved):
        chosen = saved
    elif _has_library(install_dir):
        chosen = install_dir
    elif _has_library(cwd):
        chosen = cwd
    else:
        chosen = home / "LitForge"
    chosen = chosen.expanduser().resolve()
    data = chosen / "data"
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=data, prefix=".write-test-", delete=True)  # noqa: SIM115
        probe.close()
    except OSError as exc:
        raise WorkspaceError(f"cannot write to the library folder {data}: {exc.strerror or exc}") from exc
    return chosen


def preferred_port(settings: Mapping[str, Any]) -> int:
    """The port saved for this workspace when it is free, else a free one. Keeping the port keeps the page's
    origin, and with it the preferences the page stores in the browser (theme, project, spelling words)."""
    saved = settings.get("port")
    candidates = [saved] if isinstance(saved, int) and 0 < saved < 65536 else []
    for port in [*candidates, 0]:
        with socket.socket() as s:
            if os.name != "nt":  # as the server binds: a port left in TIME_WAIT by the last session is free
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(s.getsockname()[1])
    raise WorkspaceError("no free local port")
