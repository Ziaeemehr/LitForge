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

from litforge.config import Paths
from litforge.jobs import check_cancelled, track_process

PIX2TEXT_SIZE = "~2 GB"
NEMOTRON_SIZE = "~2.5 GB model (plus CUDA PyTorch, shared with Pix2Text)"
NEMOTRON_COMMAND = "uv sync --extra formulas-gpu --extra nemotron --inexact"
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
    _uv_sync(Path(status["root"]), [status["extra"]], progress, timeout)
    return pix2text_install_status()


def nemotron_install_status(paths: Paths) -> dict[str, Any]:
    """Nemotron Parse: offered only on an NVIDIA GPU in a source install, where `uv sync` can add its extra."""
    from litforge.parsing import nemotron

    root = _install_root()
    uv = _uv_path()
    gpu = _has_nvidia_gpu(root or Path.cwd())
    sync_ok = bool(root and root.is_dir() and uv and _is_source_install(root) and _safe_project_paths(root))
    packages = nemotron.packages_available()
    reasons = []
    if not gpu:
        reasons.append("needs an NVIDIA GPU (on the processor it takes about 2 minutes per page)")
    if not packages and not sync_ok:
        reasons.append("LitForge is not a source install where uv can add packages")
    model = nemotron.model_ready(paths)
    return {
        "installed": packages and model,
        "packages": packages,
        "model": model,
        "gpu": gpu,
        "offerable": gpu and not (packages and model) and (packages or sync_ok),
        "reason": "; ".join(reasons),
        "command": NEMOTRON_COMMAND,
        "size": NEMOTRON_SIZE,
        "root": str(root) if root else None,
    }


def install_nemotron(paths: Paths, progress: Callable[[str], None] = lambda _: None, *,
                     timeout_s: float | None = None) -> dict[str, Any]:
    """Add the `nemotron` extra (with the CUDA PyTorch of formulas-gpu) and download the pinned model."""
    from litforge.parsing import nemotron

    timeout = install_timeout_s() if timeout_s is None else timeout_s
    status = nemotron_install_status(paths)
    if status["installed"]:
        progress("Nemotron Parse is already set up")
        return status
    if not status["offerable"]:
        raise ValueError("Nemotron Parse cannot be set up here: " + (status["reason"] or "unknown reason"))
    if not status["packages"]:
        _uv_sync(Path(status["root"]), ["formulas-gpu", "nemotron"], progress, timeout)
        if not nemotron.packages_available():
            raise RuntimeError("the packages were installed; restart LitForge, then set up Math: high again")
    check_cancelled()
    nemotron.download_model(paths, progress)
    return nemotron_install_status(paths)


def _uv_sync(root: Path, extras: list[str], progress: Callable[[str], None], timeout: float) -> None:
    """`uv sync --extra ... --inexact` in the source checkout, streaming its output; cancellable."""
    uv = _uv_path()
    if uv is None:
        raise ValueError("uv is no longer available")
    argv = [uv, "sync", *[a for extra in extras for a in ("--extra", extra)], "--inexact"]
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(root / ".litforge-uv-cache")
    env["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
    progress(f"Running uv sync for {', '.join(extras)} in {root}")
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
        # Read in bounded pieces: a huge line without a newline must not be held in memory.
        skipping = False
        while chunk := stream.readline(_MAX_PROGRESS_LINE):
            if not skipping:
                with suppress(queue.Full):
                    output.put_nowait(chunk.rstrip())
            skipping = not chunk.endswith("\n")  # drop the rest of an over-long line

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

    def stop_install() -> None:
        terminate(os.name == "nt")
        if os.name != "nt":
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate(True)
        process.wait()
        reader.join(timeout=1)

    with track_process(process.pid, terminate):
        try:
            while process.poll() is None:
                check_cancelled()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    message = output.get(timeout=0.2)
                except queue.Empty:
                    continue
                if message:
                    recent.append(message)
                    progress(message)
        except BaseException:
            # Timeout, cancellation or Ctrl-C: uv runs in its own session, so stop it explicitly.
            stop_install()
            raise
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
