"""Environment checks. Missing GPU or Ollama are warnings, not errors (CPU development works)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
from typing import Literal

import pymupdf
from pydantic import BaseModel

from litforge.config import Config, ModelProfileConfig, Paths
from litforge.llm.http import open_url
from litforge.parsing.docling_parser import docling_available
from litforge.parsing.mineru_parser import mineru_status
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
        with open_url(f"{url}/api/tags", timeout=2) as resp:  # noqa: S310 - local URL
            models = [m["name"] for m in json.load(resp).get("models", [])]
        detail = f"{url} reachable; models: {', '.join(models) or 'none pulled'}"
        return Check(name="ollama", level="ok" if models else "warn", detail=detail)
    except Exception as exc:  # noqa: BLE001
        return Check(name="ollama", level="warn", detail=f"{url} not reachable ({exc}); needed from screening phase")


def check_model_profile(name: str, profile: ModelProfileConfig) -> Check:
    if profile.server:
        try:
            with open_url(f"{profile.base_url}/models", timeout=2) as response:  # noqa: S310
                models = json.load(response).get("data", [])
            model = next((m for m in models if m.get("id") == profile.model), None)
            if model is None:
                return Check(name=name, level="warn", detail=f"Foreign server at {profile.base_url}: "
                             "preset model missing; change models.servers.llamacpp.port")
            modalities = (model.get("architecture") or {}).get("input_modalities")
            vision = "image" in modalities if isinstance(modalities, list) else None
            mismatch = vision is not None and vision != profile.vision
            return Check(name=name, level="warn" if mismatch else "ok",
                         detail=f"llama.cpp router: vision={vision} (configured {profile.vision}), "
                                f"status={model.get('status')}; context unavailable from router catalogue")
        except (ValueError, AttributeError, TypeError, urllib.error.HTTPError):
            return Check(name=name, level="warn",
                         detail=f"Foreign server or incompatible router at {profile.base_url}; "
                         "change models.servers.llamacpp.port")
        except Exception:
            return Check(name=name, level="warn", detail=f"llama.cpp router unavailable at {profile.base_url}")
    url = (profile.base_url or "").rstrip("/").removesuffix("/v1") + "/props"
    try:
        with open_url(url, timeout=2) as response:  # noqa: S310 - configured local server
            props = json.load(response)
        vision = (props.get("modalities") or {}).get("vision")
        context = props.get("n_ctx", (props.get("default_generation_settings") or {}).get("n_ctx"))
        mismatch = (vision is not None and bool(vision) != profile.vision or
                    context is not None and profile.context_tokens is not None and context < profile.context_tokens)
        return Check(name=name, level="warn" if mismatch else "ok",
                     detail=f"llama.cpp: vision={vision} (configured {profile.vision}), "
                            f"n_ctx={context} (configured {profile.context_tokens})")
    except Exception:
        return Check(name=name, level="warn", detail="llama.cpp /props unavailable (optional)")


def run_checks(paths: Paths, config: Config | None = None) -> list[Check]:
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
    mineru = mineru_status(paths, (config or Config()).parsing)
    checks.append(Check(name="mineru", level="ok" if mineru['installed'] else "warn",
                        detail=mineru['version'] or mineru['error'] or
                        "not installed (optional; litforge parse install-mineru)"))
    if config is not None:
        for name, profile in {"local": config.models.local, **config.models.profiles}.items():
            if (profile.provider == "openai_compatible" and not profile.remote and
                    (profile.server or profile.reasoning_param == "chat_template_kwargs") and profile.base_url):
                checks.append(check_model_profile(name, profile))
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
