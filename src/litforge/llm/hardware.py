"""Optional, bounded hardware probes; no installations or downloads."""

from __future__ import annotations

import platform
import shutil
import subprocess
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel


class Hardware(BaseModel):
    os: str
    arch: str
    ram_gb: float = 0
    unified_memory: bool = False
    gpu_name: str | None = None
    gpu_vram_gb: float = 0
    llama_server: str | None = None
    llama_version: str | None = None
    ollama: str | None = None
    free_disk_gb: float = 0


def run_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=True)
    return (result.stdout or result.stderr).strip()


def _windows_ram() -> int:
    import ctypes
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                    *[(name, ctypes.c_ulonglong) for name in
                      ("physical", "available", "page", "available_page", "virtual", "available_virtual", "extended")]]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.physical)


def detect_hardware(data_root: Path, *, runner: Callable[[list[str]], str] = run_command,
                    which: Callable[[str], str | None] = shutil.which, system: str | None = None,
                    arch: str | None = None, read_meminfo: Callable[[], str] | None = None,
                    windows_ram: Callable[[], int] = _windows_ram) -> Hardware:
    system, arch = system or platform.system(), arch or platform.machine()
    hw = Hardware(os=system, arch=arch, unified_memory=system == "Darwin" and arch == "arm64")
    try:
        if system == "Darwin":
            hw.ram_gb = int(runner(["sysctl", "-n", "hw.memsize"])) / 1024**3
        elif system == "Linux":
            content = read_meminfo() if read_meminfo else Path("/proc/meminfo").read_text()
            hw.ram_gb = int(next(line.split()[1] for line in content.splitlines()
                                if line.startswith("MemTotal:"))) / 1024**2
        elif system == "Windows":
            hw.ram_gb = windows_ram() / 1024**3
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        pass
    if which("nvidia-smi"):
        try:
            rows = runner(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
            gpus = [(float(row.rsplit(",", 1)[1]) / 1024, row.rsplit(",", 1)[0].strip())
                    for row in rows.splitlines() if "," in row]
            if gpus:
                hw.gpu_vram_gb, hw.gpu_name = max(gpus)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    hw.llama_server, hw.ollama = which("llama-server"), which("ollama")
    if hw.llama_server:
        with suppress(OSError, subprocess.SubprocessError):
            hw.llama_version = runner([hw.llama_server, "--version"])[:200]
    try:
        parent = data_root.resolve()
        while not parent.exists():
            parent = parent.parent
        hw.free_disk_gb = shutil.disk_usage(parent).free / 1024**3
    except OSError:
        pass
    return hw
