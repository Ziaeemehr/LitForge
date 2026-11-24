"""Create or update LitForge's writable runtime from a read-only packaged app folder.

Runs under any Python >= 3.9 with the standard library only; a native installer starts it through the bundled
uv (`uv run --no-project --python 3.12 provision.py ...`), so a clean machine needs neither Python nor uv.

Layout (Task 7 of docs/superpowers/plans/2026-10-23-desktop-app.md):
  APP      read-only: pyproject.toml, uv.lock, src/, configs/, LICENSE, README.md, manifest.json
  RUNTIME  writable, per user: venv/, python/ (uv-managed CPython), cache/, bin/uv, state.json

`uv sync` is idempotent, so an interrupted or failed run is repaired by running it again; state.json, which
says the runtime is ready, is written only after a sync succeeds. The venv is synced in place, because its
scripts record their absolute path and cannot be renamed. Library data is never under RUNTIME.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PYTHON = "3.12"


def uv_env(runtime: Path, venv: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "UV_PROJECT_ENVIRONMENT": str(venv),
        "UV_CACHE_DIR": str(runtime / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(runtime / "python"),
        "UV_PYTHON_PREFERENCE": "only-managed",  # never bind the runtime to a system Python that may change
        "UV_NO_CONFIG": "1",  # a user's uv.toml must not redirect indexes or paths
    })
    for key in ("VIRTUAL_ENV", "UV_FROZEN", "UV_LOCKED"):
        env.pop(key, None)
    return env


def sync_command(uv: str, extras: list[str]) -> list[str]:
    # --inexact: an update keeps optional tools the user added later (Settings, Optional tools).
    argv = [uv, "sync", "--frozen", "--no-dev", "--inexact", "--python", PYTHON]
    for extra in extras:
        argv += ["--extra", extra]
    return argv


def provision(app: Path, runtime: Path, uv: str, extras: list[str]) -> Path:
    """Sync RUNTIME/venv for APP with EXTRAS; returns the venv path. Raises on failure; running it again repairs."""
    manifest = json.loads((app / "manifest.json").read_text(encoding="utf-8"))
    runtime.mkdir(parents=True, exist_ok=True)
    venv, state_file = runtime / "venv", runtime / "state.json"
    state_file.unlink(missing_ok=True)  # "ready" is claimed again only after this sync succeeds
    result = subprocess.run(sync_command(uv, extras), cwd=app, env=uv_env(runtime, venv), stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"uv sync failed with exit code {result.returncode}; run the setup again to retry")
    # LitForge adds optional tools later with this uv (litforge.distribution.bundled_uv), even if APP moves.
    (runtime / "bin").mkdir(exist_ok=True)
    bundled = runtime / "bin" / Path(uv).name
    bundled.unlink(missing_ok=True)  # the earlier copy keeps the read-only mode of a mounted or installed app
    shutil.copy2(uv, bundled)
    state = {"app_version": manifest["app_version"], "target": manifest["target"], "extras": sorted(set(extras))}
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return venv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--uv", required=True)
    parser.add_argument("--extra", action="append", default=[], dest="extras")
    args = parser.parse_args(argv)
    try:
        venv = provision(args.app.resolve(), args.runtime.resolve(), args.uv, args.extras)
    except (OSError, RuntimeError, KeyError, ValueError) as error:
        print(f"LitForge setup failed: {error}", file=sys.stderr)
        return 1
    print(f"LitForge runtime ready: {venv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
