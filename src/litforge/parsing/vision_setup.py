"""Getting "better math: high" ready: Ollama installed and running, and the vision model pulled.

`status()` says what is missing, what this computer has (GPU, memory, free disk) and whether LitForge can
install Ollama itself; the page shows it and, once the user agrees, `run()` does the missing steps.

A GPU is not required: Ollama runs the model on the processor too, only several times slower. So no GPU is
a warning (with "medium" suggested), while too little memory or disk is a blocker.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MIN_MEMORY_GB = 8  # qwen2.5vl:7b takes about 6 GB once loaded
MIN_DISK_GB = 10  # the model (about 6 GB) plus Ollama itself
MODEL_GB = 6
CPU_WARNING = ("No GPU found: the model runs on the processor. That works, but slowly: about a minute or more "
               "per page instead of about 12 s. Math: medium is the faster choice on this computer.")
LINUX_SCRIPT = "curl -fsSL https://ollama.com/install.sh | sh"
MAC_ZIP = "https://ollama.com/download/Ollama-darwin.zip"
WINDOWS_SETUP = "https://ollama.com/download/OllamaSetup.exe"

Progress = Callable[[str], None]


@dataclass
class SetupStatus:
    model: str
    url: str
    local: bool  # Ollama is meant to run on this computer, so LitForge may install and start it
    installed: bool
    running: bool
    model_ready: bool
    install: str | None = None  # how LitForge installs Ollama here: brew | mac-app | windows | linux | None
    gpu: str | None = None  # "NVIDIA ...", "Apple Silicon" or None: CPU only
    memory_gb: float | None = None
    disk_free_gb: float | None = None
    blockers: list[str] = field(default_factory=list)  # setting up is pointless, use medium
    warnings: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)  # commands for the user when LitForge cannot do a step

    @property
    def ready(self) -> bool:
        return self.running and self.model_ready

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ready": self.ready,
                "requirements": {"memory_gb": MIN_MEMORY_GB, "disk_gb": MIN_DISK_GB, "model_gb": MODEL_GB}}


class OllamaSetup:
    def __init__(self, url: str, model: str, sleep: Callable[[float], None] = time.sleep) -> None:
        self.url, self.model, self.sleep = url.rstrip("/"), model, sleep

    # ---- what is there -------------------------------------------------------

    def models(self) -> set[str] | None:
        """Names of the pulled models, or None when Ollama does not answer."""
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=2) as resp:  # noqa: S310 - local URL
                return {str(m.get("name")) for m in json.load(resp).get("models", [])}
        except (OSError, ValueError):
            return None

    def status(self) -> SetupStatus:
        models = self.models()
        running = models is not None
        model_ready = running and bool({self.model, f"{self.model}:latest"} & (models or set()))
        local = urlsplit(self.url).hostname in ("localhost", "127.0.0.1", "::1")
        st = SetupStatus(model=self.model, url=self.url, local=local, installed=running or find_ollama() is not None,
                         running=running, model_ready=model_ready)
        if st.ready:
            return st  # nothing to set up: skip the slower hardware checks
        if not local and not running:
            st.blockers.append(f"Ollama at {self.url} (LITFORGE_OLLAMA_URL) does not answer; start it on that "
                               "computer, or remove the setting to use one on this computer.")
            return st
        if not local:  # a running Ollama elsewhere: only the model is missing, pulled over the network
            st.manual.append(f"ollama pull {self.model}")
            return st
        st.gpu, st.memory_gb, st.disk_free_gb = gpu_name(), memory_gb(), disk_free_gb()
        if not st.installed:
            st.install = install_method()
            if st.install is None:
                st.manual.append(LINUX_SCRIPT if sys.platform.startswith("linux") else "https://ollama.com/download")
        if not model_ready:
            st.manual.append(f"ollama pull {self.model}")
        if st.memory_gb is not None and st.memory_gb < MIN_MEMORY_GB:
            st.blockers.append(f"This computer has {st.memory_gb:.0f} GB of memory; the model needs at least "
                               f"{MIN_MEMORY_GB} GB. Use Math: medium instead.")
        if not model_ready and st.disk_free_gb is not None and st.disk_free_gb < MIN_DISK_GB:
            st.blockers.append(f"Only {st.disk_free_gb:.0f} GB of free disk; the download needs about "
                               f"{MIN_DISK_GB} GB.")
        if st.gpu is None:
            st.warnings.append(CPU_WARNING)
        return st

    # ---- doing it ------------------------------------------------------------

    def run(self, progress: Progress) -> None:
        """Install Ollama if needed, start it, pull the model."""
        st = self.status()
        if st.ready:
            progress(f"{self.model} is ready")
            return
        if st.blockers:
            raise RuntimeError(st.blockers[0])
        if not st.running:
            if not st.installed:
                if st.install is None:
                    raise RuntimeError("LitForge cannot install Ollama here; run in a terminal: "
                                       + " && ".join(st.manual))
                install_ollama(st.install, progress)
            self.start(progress)
        self.pull(progress)
        progress(f"done: {self.model} is ready for better math (high)")

    def start(self, progress: Progress, wait_s: float = 60) -> None:
        if self.models() is None:  # an installer may have started it already
            for _ in range(5):
                self.sleep(1)
                if self.models() is not None:
                    return
            progress("starting Ollama")
            start_ollama(self.url)
        for _ in range(int(wait_s)):
            if self.models() is not None:
                return
            self.sleep(1)
        raise RuntimeError(f"Ollama did not start at {self.url}; open the Ollama app, then try again")

    def pull(self, progress: Progress) -> None:
        """Download the model through Ollama, reporting every 5%."""
        progress(f"downloading {self.model} (about {MODEL_GB} GB)")
        req = urllib.request.Request(f"{self.url}/api/pull", method="POST",
                                     data=json.dumps({"model": self.model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        layers: dict[str, tuple[int, int]] = {}
        shown = (0, -1)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 - local URL
                for line in resp:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(f"Ollama could not download {self.model}: {event['error']}")
                    if event.get("digest") and event.get("total"):
                        layers[event["digest"]] = (int(event.get("completed") or 0), int(event["total"]))
                        done, total = (sum(v[i] for v in layers.values()) for i in (0, 1))
                        step = (total, int(100 * done / total) // 5 * 5)  # a new layer changes the total
                        if step != shown:
                            shown = step
                            progress(f"downloading {self.model}: {step[1]}% of {total / 1e9:.1f} GB")
                    if event.get("status") == "success":
                        return
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Ollama error {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"download of {self.model} interrupted ({exc}); try again to resume") from exc
        raise RuntimeError(f"download of {self.model} ended early; try again to resume")


# ---- this computer -------------------------------------------------------------


def gpu_name() -> str | None:
    """The GPU Ollama can use, or None (CPU only). AMD cards are not detected: they are reported as CPU."""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            if out:
                return "NVIDIA " + out.splitlines()[0].removeprefix("NVIDIA ")
        except (subprocess.SubprocessError, OSError):
            pass
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "Apple Silicon (shares the computer's memory)"
    return None


def memory_gb() -> float | None:
    try:
        if sys.platform == "win32":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
            return float(status.ullTotalPhys) / 2**30
        return float(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / 2**30
    except (OSError, ValueError, AttributeError):
        return None


def disk_free_gb() -> float | None:
    """Free space where Ollama keeps its models (OLLAMA_MODELS, else the home folder)."""
    target = Path(os.environ.get("OLLAMA_MODELS") or Path.home())
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return shutil.disk_usage(target).free / 1e9
    except OSError:
        return None


def _mac_app() -> Path | None:
    for base in (Path("/Applications"), Path.home() / "Applications"):
        if (base / "Ollama.app").is_dir():
            return base / "Ollama.app"
    return None


def find_ollama() -> str | None:
    """The ollama program, also where a launcher without the shell's PATH would not see it."""
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    if app := _mac_app():
        candidates.append(app / "Contents" / "Resources" / "ollama")
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe")
    return next((str(p) for p in candidates if p.is_file()), None)


def _sudo_without_password() -> bool:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if not shutil.which("sudo"):
        return False
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def install_method() -> str | None:
    if sys.platform == "darwin":
        return "brew" if _brew() else "mac-app"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux") and shutil.which("curl") and _sudo_without_password():
        return "linux"  # Ollama's script installs to /usr and needs root; there is no password prompt here
    return None


def _brew() -> str | None:
    return shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                         if Path(p).is_file()), None)


# ---- installing and starting ---------------------------------------------------


def _run(cmd: list[str], progress: Progress) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,  # noqa: S603
                            text=True, errors="replace")
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        if line.strip():
            tail = [*tail[-4:], line.strip()]
            progress(line.strip()[:200])
    if proc.wait() != 0:
        raise RuntimeError(f"{cmd[0]} failed: " + " | ".join(tail))


def _download(url: str, dest: Path, progress: Progress) -> None:
    progress(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as out:  # noqa: S310 - fixed https URL
        total, done, shown = int(resp.headers.get("Content-Length") or 0), 0, -10
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total and (pct := 100 * done // total) >= shown + 10:
                shown = pct - pct % 10
                progress(f"downloading Ollama: {shown}%")


def install_ollama(method: str, progress: Progress) -> None:
    progress("installing Ollama")
    if method == "brew":
        _run([_brew() or "brew", "install", "ollama"], progress)
    elif method == "mac-app":
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "Ollama-darwin.zip"
            _download(MAC_ZIP, archive, progress)
            target = Path.home() / "Applications"
            target.mkdir(exist_ok=True)
            _run(["ditto", "-x", "-k", str(archive), str(target)], progress)  # keeps the app's signature intact
    elif method == "windows":
        with tempfile.TemporaryDirectory() as tmp:
            setup = Path(tmp) / "OllamaSetup.exe"
            _download(WINDOWS_SETUP, setup, progress)
            _run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], progress)  # per user, no admin
    elif method == "linux":
        _run(["sh", "-c", LINUX_SCRIPT], progress)
    else:
        raise ValueError(f"unknown install method {method}")
    if find_ollama() is None and _mac_app() is None:
        raise RuntimeError("Ollama was installed but cannot be found; open it once, then try again")
    progress("Ollama installed")


def start_ollama(url: str) -> None:
    """Start the Ollama server in the background; it keeps running after LitForge stops."""
    if sys.platform == "darwin" and (app := _mac_app()):
        subprocess.Popen(["open", "-g", str(app)])  # noqa: S603, S607 - the app runs the server and its menu icon
        return
    exe = find_ollama()
    if exe is None:
        raise RuntimeError("Ollama is not installed")
    parts = urlsplit(url)
    env = {**os.environ, "OLLAMA_HOST": f"{parts.hostname}:{parts.port or 11434}"}
    log = Path(tempfile.gettempdir()) / "litforge-ollama.log"
    with log.open("ab") as out:
        kwargs: dict[str, Any] = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
            if sys.platform == "win32" else {"start_new_session": True})
        subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,  # noqa: S603
                         env=env, **kwargs)
