"""Status and installation helpers for optional LitForge tools."""

from __future__ import annotations

import importlib
import importlib.metadata
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from litforge.jobs import check_cancelled, track_process

PIX2TEXT_SIZE = "~2 GB"
PIX2TEXT_INSTALL_TIMEOUT_S = 45 * 60
_MAX_PROGRESS_LINE = 500


def _package_file() -> Path:
    import litforge

    return Path(litforge.__file__).resolve()


def _install_root() -> Path | None:
    package_file = _package_file()
    for parent in package_file.parents:
        project = parent / "pyproject.toml"
        if not project.is_file():
            continue
        try:
            with project.open("rb") as stream:
                if tomllib.load(stream).get("project", {}).get("name") == "litforge":
                    return parent
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return None


def _uv_path() -> str | None:
    if found := shutil.which("uv"):
        return found
    adjacent = Path(sys.executable).with_name("uv.exe" if os.name == "nt" else "uv")
    return str(adjacent) if adjacent.is_file() and (os.name == "nt" or os.access(adjacent, os.X_OK)) else None


def _is_source_install(root: Path) -> bool:
    try:
        _package_file().relative_to(root / "src" / "litforge")
        return True
    except ValueError:
        return False


def _has_nvidia_gpu(root: Path) -> bool:
    from litforge.llm.hardware import detect_hardware

    return detect_hardware(root).gpu_name is not None


def _safe_project_paths(root: Path) -> bool:
    resolved_root = root.resolve()
    for path in (root / ".venv", root / ".litforge-uv-cache", root / "uv.lock"):
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError:
            return False
    return True


def pix2text_install_status() -> dict[str, Any]:
    try:
        version: str | None = importlib.metadata.version("pix2text")
    except importlib.metadata.PackageNotFoundError:
        version = None
    root = _install_root()
    uv = _uv_path()
    editable = bool(root and _is_source_install(root))
    gpu = _has_nvidia_gpu(root or Path.cwd())
    extra = "formulas-gpu" if gpu else "formulas"
    command = f"uv sync --extra {extra} --inexact"
    safe_paths = bool(root and _safe_project_paths(root))
    offerable = version is None and bool(root and root.is_dir() and uv and editable and safe_paths)
    reasons = []
    if not root:
        reasons.append("LitForge source root not found")
    elif not editable:
        reasons.append("LitForge is not installed from source")
    if not uv:
        reasons.append("uv is not available")
    if root and not safe_paths:
        reasons.append("the project virtual environment or uv cache points outside the install root")
    return {
        "installed": version is not None,
        "version": version,
        "offerable": offerable,
        "available": bool(root and editable and uv and safe_paths),
        "reason": "; ".join(reasons),
        "command": command,
        "extra": extra,
        "gpu": gpu,
        "size": PIX2TEXT_SIZE,
        "root": str(root) if root else None,
    }


def install_timeout_s() -> float:
    raw = os.environ.get("LITFORGE_PIX2TEXT_INSTALL_TIMEOUT_S")
    if raw is None:
        return PIX2TEXT_INSTALL_TIMEOUT_S
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError("LITFORGE_PIX2TEXT_INSTALL_TIMEOUT_S must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("LITFORGE_PIX2TEXT_INSTALL_TIMEOUT_S must be a positive number")
    return timeout


def install_pix2text(progress: Callable[[str], None] = lambda _: None, *,
                     timeout_s: float | None = None) -> dict[str, Any]:
    """Install the matching extra into the active source checkout's environment."""
    timeout = install_timeout_s() if timeout_s is None else timeout_s
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Pix2Text installation timeout must be positive")
    status = pix2text_install_status()
    if not status["offerable"]:
        raise ValueError("Pix2Text cannot be installed here; run `" + status["command"] +
                         "` in the LitForge source root")
    root = Path(status["root"])
    uv = _uv_path()
    if uv is None:
        raise ValueError("uv is no longer available")
    argv = [uv, "sync", "--extra", status["extra"], "--inexact"]
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(root / ".litforge-uv-cache")
    env["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
    progress(f"Running uv sync for {status['extra']} in {root}")
    output: queue.Queue[str] = queue.Queue(maxsize=200)
    recent: deque[str] = deque(maxlen=12)
    process_options: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                               **process_options)
    stream = process.stdout
    assert stream is not None

    def read_output() -> None:
        for line in stream:
            text = line.rstrip()[:_MAX_PROGRESS_LINE]
            try:
                output.put_nowait(text)
            except queue.Full:
                continue

    reader = threading.Thread(target=read_output, daemon=True, name="litforge-pix2text-output")
    reader.start()
    deadline = time.monotonic() + timeout
    def terminate(force: bool) -> None:
        if os.name == "nt":
            if force:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            elif process.poll() is None:
                process.terminate()
        else:
            sig = signal.SIGKILL if force else signal.SIGTERM
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, sig)

    with track_process(process.pid, terminate):
        while process.poll() is None:
            check_cancelled()
            if time.monotonic() >= deadline:
                terminate(os.name == "nt")
                if os.name != "nt":
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        terminate(True)
                process.wait()
                reader.join(timeout=1)
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                message = output.get(timeout=0.2)
            except queue.Empty:
                continue
            if message:
                recent.append(message)
                progress(message)
    reader.join(timeout=1)
    while True:
        try:
            message = output.get_nowait()
        except queue.Empty:
            break
        if message:
            recent.append(message)
            progress(message)
    if process.returncode:
        detail = "\n".join(recent)
        raise RuntimeError(f"uv sync failed with exit code {process.returncode}" + (f": {detail}" if detail else ""))
    importlib.invalidate_caches()
    return pix2text_install_status()
