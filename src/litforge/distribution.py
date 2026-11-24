"""Where LitForge may write next to its program: the source checkout, or a native installer's runtime folder.

A native installer ships the program read-only and builds the Python environment in a per-user runtime folder
(packaging/desktop/provision.py), which marks it with state.json beside the venv. Anything that changes the
environment (optional tools, the Linux desktop engine) must then write there and keep uv.lock as shipped.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def packaged_runtime() -> Path | None:
    """The runtime folder when this Python runs from a native installer's environment, else None."""
    runtime = Path(sys.prefix).parent
    return runtime if (runtime / "state.json").is_file() else None


def bundled_uv() -> str | None:
    """The uv a native installer copied into the runtime; preferred there, as it matches the shipped uv.lock."""
    runtime = packaged_runtime()
    uv = runtime / "bin" / ("uv.exe" if os.name == "nt" else "uv") if runtime else None
    return str(uv) if uv and uv.is_file() else None


def writable_root(root: Path) -> Path:
    """Folder for files LitForge adds beside its program (desktop host env, downloaded libraries)."""
    return packaged_runtime() or root


def uv_sync_env(root: Path, base: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Extra `uv sync` flags and the environment for the project at ROOT, so it changes the running environment only.

    The runtime values match provision.py, which cannot import LitForge because it runs before it is installed.
    """
    env = dict(os.environ if base is None else base)
    env.pop("VIRTUAL_ENV", None)
    runtime = packaged_runtime()
    if runtime is None:
        env.update(UV_CACHE_DIR=str(root / ".litforge-uv-cache"), UV_PROJECT_ENVIRONMENT=str(root / ".venv"))
        return [], env
    env.update(UV_PROJECT_ENVIRONMENT=sys.prefix, UV_CACHE_DIR=str(runtime / "cache"),
               UV_PYTHON_INSTALL_DIR=str(runtime / "python"), UV_PYTHON_PREFERENCE="only-managed", UV_NO_CONFIG="1")
    return ["--frozen"], env
