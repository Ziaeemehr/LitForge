"""Managed llama.cpp router. All external operations can be replaced in tests."""

from __future__ import annotations

import configparser
import csv
import errno
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from litforge.config import LlamaCppServerConfig
from litforge.llm.files import atomic_write, file_lock
from litforge.llm.hardware import run_command
from litforge.llm.model_setup import INSTALL_LLAMA


class ProcessIdentity(BaseModel):
    pid: int
    started: str
    command: list[str]


def http_probe(url: str) -> dict[str, Any] | None:
    # Bypass proxy environment variables: these requests must stay on loopback.
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=1) as response:
            result = json.load(response)
        return result if isinstance(result, dict) else {}
    except urllib.error.HTTPError:
        return {}  # A listener exists, even if it is not a llama.cpp router.
    except urllib.error.URLError as exc:
        return None if getattr(exc.reason, "errno", None) == errno.ECONNREFUSED else {}
    except (ValueError, OSError):
        return {}


def spawn_process(command: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": stream, "stderr": stream,
                                   "close_fds": True}
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            options["creationflags"] = flags
        else:
            options["start_new_session"] = True
        return subprocess.Popen(command, **options).pid


def process_identity(pid: int) -> ProcessIdentity | None:
    """Fail closed when OS process identity cannot be established. No shell commands."""
    if pid <= 0:
        return None
    try:
        if os.name == "nt":
            script = (f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | "
                      "Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress")
            data = json.loads(run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script]))
            return ProcessIdentity(pid=int(data["ProcessId"]), started=str(data["CreationDate"]),
                                   command=shlex.split(data["CommandLine"].replace("\\", "/")))
        proc = Path(f"/proc/{pid}")
        if proc.exists():
            # Linux start ticks distinguish a reused pid, even within one second.
            started = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
            command = proc.joinpath("cmdline").read_bytes().decode().rstrip("\0").split("\0")
        else:
            started = run_command(["ps", "-p", str(pid), "-o", "lstart="])
            executable = run_command(["ps", "-ww", "-p", str(pid), "-o", "comm="])
            raw = run_command(["ps", "-ww", "-p", str(pid), "-o", "args="])
            # ps does not escape spaces in arguments. Preserve the preset between our long options.
            preset = raw.partition(" --models-preset ")[2].partition(" --")[0]
            if preset.startswith(("\"", "'")):
                preset = shlex.split(preset)[0]
            command = [executable, "--models-preset", preset] if preset else [executable]
        return ProcessIdentity(pid=pid, started=started, command=command) if started and command else None
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return None


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            output = run_command(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
            return any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(output.splitlines()))
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, subprocess.SubprocessError):
        return True  # Inability to inspect a process must never authorize a second spawn.


def terminate_process(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def kill_process(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


class LlamaCppServer:
    def __init__(self, root: Path, *, probe: Callable[[str], dict[str, Any] | None] = http_probe,
                 spawner: Callable[[list[str], Path], int] = spawn_process,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 which: Callable[[str], str | None] = shutil.which,
                 process: Callable[[int], ProcessIdentity | None] = process_identity,
                 alive: Callable[[int], bool] = process_alive,
                 terminate: Callable[[int], None] = terminate_process,
                 kill: Callable[[int], None] = kill_process,
                 lock: Callable[..., AbstractContextManager[None]] = file_lock) -> None:
        self.root = root.resolve()
        self.probe, self.spawner, self.clock, self.sleep = probe, spawner, clock, sleep
        self.which, self.process, self.terminate, self.lock = which, process, terminate, lock
        self.alive, self.kill = alive, kill
        self.state = self.root / "state" / "llama-server.json"
        self.lock_path = self.root / "state" / "llama-server.lock"

    def _preset(self, cfg: LlamaCppServerConfig) -> str:
        return str((self.root / cfg.preset).resolve())

    def _models(self, cfg: LlamaCppServerConfig) -> list[dict[str, Any]] | None:
        health = self.probe(cfg.base_url.removesuffix("/v1") + "/health")
        if health is None:
            return None
        models = self.probe(cfg.base_url + "/models") if health.get("status") == "ok" else {}
        if models is not None and isinstance(models.get("data"), list):
            return [m for m in models["data"] if isinstance(m, dict)]
        return []

    @staticmethod
    def _cold(models: list[dict[str, Any]], model: str) -> bool:
        status = next(m.get("status") for m in models if m.get("id") == model)
        value = status.get("value") if isinstance(status, dict) else status
        return value not in {"loaded", "ready"}

    def _foreign(self, cfg: LlamaCppServerConfig) -> None:
        from litforge.analysis.gateway import GatewayError
        raise GatewayError(f"port {cfg.port} is used by another server; change models.servers.llamacpp.port")

    def ensure_running(self, cfg: LlamaCppServerConfig, model: str) -> bool:
        """Return whether this model may need a first-load timeout. Never load a model here."""
        from litforge.analysis.gateway import GatewayError
        models = self._models(cfg)
        if models is not None:
            if any(m.get("id") == model for m in models):
                return self._cold(models, model)
            self._foreign(cfg)
        if not cfg.managed:
            raise GatewayError("llama-server is not running; start it or enable models.servers.llamacpp.managed")
        try:
            with self.lock(self.lock_path, cfg.start_timeout_s, clock=self.clock, sleep=self.sleep):
                # Another LitForge process may have finished startup while we waited.
                models = self._models(cfg)
                if models is not None:
                    if any(m.get("id") == model for m in models):
                        return self._cold(models, model)
                    self._foreign(cfg)
                if self._owned(cfg) is None:
                    if self._unverified_live_state():
                        raise GatewayError("cannot verify the recorded live llama-server; "
                                           "inspect state/llama-server.json before starting another server")
                    executable = self.which(cfg.command)
                    if executable is None:
                        raise GatewayError("llama-server executable missing; install with " + INSTALL_LLAMA)
                    command = [executable, "--models-preset", self._preset(cfg), "--models-max", str(cfg.models_max),
                               "--host", cfg.host, "--port", str(cfg.port), "--sleep-idle-seconds",
                               str(int(cfg.idle_unload_minutes * 60))]
                    try:
                        pid = self.spawner(command, self.root / "logs" / "llama-server.log")
                    except FileNotFoundError:
                        raise GatewayError("llama-server executable missing; install with " + INSTALL_LLAMA) from None
                    identity = self.process(pid)
                    atomic_write(self.state, json.dumps({"pid": pid, "started": identity.started if identity else None,
                                                        "preset": self._preset(cfg), "command": command}))
                deadline = self.clock() + cfg.start_timeout_s
                while self.clock() < deadline:
                    models = self._models(cfg)
                    if models and any(m.get("id") == model for m in models):
                        return True
                    self.sleep(min(0.2, max(0, deadline - self.clock())))
                raise GatewayError("llama-server start timed out; see logs/llama-server.log")
        except TimeoutError as exc:
            raise GatewayError(str(exc)) from None

    def _unverified_live_state(self) -> bool:
        try:
            data = json.loads(self.state.read_text())
            pid = int(data["pid"])
            identity = self.process(pid)
            if identity is None:
                return self.alive(pid)
            return data["started"] is None or identity.started == data["started"]
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _owned(self, cfg: LlamaCppServerConfig) -> ProcessIdentity | None:
        try:
            data = json.loads(self.state.read_text())
            pid = int(data["pid"])
            identity = self.process(pid)
            if identity is None or identity.pid != pid or identity.started != data["started"]:
                return None
            if data["preset"] != self._preset(cfg):
                return None
            command = identity.command
            if not command or Path(command[0]).name.lower() not in {"llama-server", "llama-server.exe"}:
                return None
            index = command.index("--models-preset")
            if Path(command[index + 1]) != Path(self._preset(cfg)):
                return None
            return identity
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None

    def stop(self, cfg: LlamaCppServerConfig) -> bool:
        with self.lock(self.lock_path, cfg.start_timeout_s, clock=self.clock, sleep=self.sleep):
            identity = self._owned(cfg)
            if identity is None:
                return False
            # Revalidate immediately before each signal. A residual OS-level race remains
            # between inspecting identity and kill(pid): portable stdlib has no process handle
            # that atomically verifies start time and signals on every supported platform.
            if self._owned(cfg) != identity:
                return False
            try:
                self.terminate(identity.pid)
            except ProcessLookupError:
                return False
            self.sleep(5.0)
            if self._owned(cfg) == identity:
                with suppress(ProcessLookupError):
                    self.kill(identity.pid)
            # Retain identity until exit: a concurrent start must not race a still-stopping process.
            return True

    def status(self, cfg: LlamaCppServerConfig) -> dict[str, Any]:
        models = self._models(cfg)
        foreign = models == []
        preset = configparser.ConfigParser(interpolation=None)
        expected: list[str] = []
        try:
            preset.read(self._preset(cfg))
            expected = preset.sections()
            if models:
                foreign = bool(expected and set(expected).isdisjoint(m.get("id") for m in models))
        except (OSError, configparser.Error):
            pass
        return {"running": bool(models) and not foreign, "foreign": foreign,
                "owned": self._owned(cfg) is not None, "models": models or [],
                "preset_models": expected, "base_url": cfg.base_url}


def status_line(report: dict[str, Any]) -> str:
    """Human summary contains only preset aliases and simple load states."""
    if report.get("foreign"):
        return f"Foreign server at {report['base_url']}; change models.servers.llamacpp.port."
    if not report["running"]:
        return str(report.get("detail", "llama-server stopped"))
    parts = ["llama-server running " + ("(managed by LitForge)" if report.get("owned") else "(external)")]
    models = {m.get("id"): m for m in report["models"]}
    for name in report.get("preset_models", []):
        status = models.get(name, {}).get("status", "unknown")
        value = status.get("value", "unknown") if isinstance(status, dict) else status
        parts.append(f"{name}: {value}")
    return " · ".join(parts)
