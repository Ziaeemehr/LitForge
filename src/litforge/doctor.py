"""Environment checks. Missing GPU or Ollama are warnings, not errors (CPU development works)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from typing import Literal

import pymupdf
from pydantic import BaseModel

from litforge.config import Paths
from litforge.parsing.docling_parser import docling_available
from litforge.storage.db import connect, fts5_available, latest_version, schema_version

Level = Literal["ok", "warn", "error"]


class Check(BaseModel):
    name: str
    level: Level
    detail: str


def _gpu() -> Check:
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            return Check(name="gpu", level="ok", detail=out)
        except (subprocess.SubprocessError, OSError) as exc:
            return Check(name="gpu", level="warn", detail=f"nvidia-smi failed: {exc}")
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return Check(name="gpu", level="warn",
                     detail="Apple Silicon, no CUDA; VRAM benchmarks must run on the NVIDIA machine")
    return Check(name="gpu", level="warn", detail="no NVIDIA GPU detected; CPU only")


def _ollama(url: str) -> Check:
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=2) as resp:  # noqa: S310 - local URL
            models = [m["name"] for m in json.load(resp).get("models", [])]
        detail = f"{url} reachable; models: {', '.join(models) or 'none pulled'}"
        return Check(name="ollama", level="ok" if models else "warn", detail=detail)
    except Exception as exc:  # noqa: BLE001
        return Check(name="ollama", level="warn", detail=f"{url} not reachable ({exc}); needed from screening phase")


def run_checks(paths: Paths) -> list[Check]:
    checks = [
        Check(name="python", level="ok" if sys.version_info[:2] == (3, 12) else "warn",
              detail=platform.python_version()),
        Check(name="pymupdf", level="ok", detail=pymupdf.__version__),
        Check(name="sqlite", level="ok" if fts5_available() else "error",
              detail=f"{sqlite3.sqlite_version}, FTS5 {'available' if fts5_available() else 'MISSING'}"),
        Check(name="docling", level="ok" if docling_available() else "warn",
              detail="installed" if docling_available() else "not installed (optional extra)"),
        _gpu(),
        _ollama(os.environ.get("LITFORGE_OLLAMA_URL", "http://localhost:11434")),
    ]
    try:
        paths.ensure()
        probe = paths.root / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        free_gb = shutil.disk_usage(paths.root).free / 1e9
        checks.append(Check(name="data_dir", level="ok" if free_gb > 5 else "warn",
                            detail=f"{paths.root} writable, {free_gb:.0f} GB free"))
    except OSError as exc:
        checks.append(Check(name="data_dir", level="error", detail=f"{paths.root}: {exc}"))
        return checks
    conn = connect(paths.db)
    current, latest = schema_version(conn), latest_version()
    conn.close()
    checks.append(Check(
        name="database", level="ok" if current == latest else "warn",
        detail=f"schema v{current} (latest v{latest})" + ("" if current == latest else "; run any command to migrate"),
    ))
    return checks
