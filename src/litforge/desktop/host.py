"""The desktop window: shows a loading page at once, starts the LitForge backend, then navigates to it.

Standard library and pywebview only, and Python 3.9+. On Linux this file may run under the system Python,
whose own `gi` gives the WebKitGTK engine, while the backend runs in LitForge's environment
(`litforge desktop` decides; see the desktop plan). Run directly: host.py --gui gtk --log FILE -- BACKEND...
"""

from __future__ import annotations

import argparse
import ctypes
import html
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
import types
from collections.abc import Sequence
from pathlib import Path

START_TIMEOUT = 90.0  # a first start may migrate the database
LOADING_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><style>"
    "body{margin:0;height:100vh;display:grid;place-items:center;font:15px system-ui,sans-serif;"
    "color:#555;background:#fafaf8}@media (prefers-color-scheme:dark){body{color:#bbb;background:#1c1c1e}}"
    "b{color:#c2410c}</style></head><body><div><b>Lit</b>Forge is starting…</div></body></html>"
)


class BackendError(Exception):
    pass


def _sibling(name: str) -> types.ModuleType:
    """Load a module next to this file by path: under the system Python `litforge` is not importable."""
    spec = importlib.util.spec_from_file_location(f"_litforge_desktop_{name}", Path(__file__).with_name(name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


instance = _sibling("instance")
close = _sibling("close")


def _log(log_path: Path, text: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def _tail(log_path: Path, since: int, limit: int = 1500) -> str:
    try:
        with open(log_path, "rb") as f:
            f.seek(since)
            return f.read().decode("utf-8", "replace")[-limit:].strip()
    except OSError:
        return ""


class Backend:
    """The `litforge desktop-server` child: prints one JSON line with its URL, and exits when its stdin closes,
    so it never outlives the window. Its stderr goes to the log file."""

    def __init__(self, cmd: Sequence[str], log_path: Path) -> None:
        self.cmd = list(cmd)
        self.log_path = Path(log_path)
        self.proc: subprocess.Popen[str] | None = None
        self.stopping = threading.Event()  # it said it is shutting down (Quit, or the window asked it to)

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def exited(self) -> bool:
        return self.returncode is not None

    def finishing(self) -> bool:
        """Stopping or stopped: its window can close now, even while running tasks are still being cancelled."""
        return self.stopping.is_set() or self.exited()

    def start(self, timeout: float = START_TIMEOUT) -> str:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        _log(self.log_path, "starting the backend: " + " ".join(self.cmd))
        since = self.log_path.stat().st_size
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        with open(self.log_path, "a", encoding="utf-8") as log:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                         text=True, encoding="utf-8", creationflags=flags)
        ready: list[str] = []
        got = threading.Event()

        def read() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for line in self.proc.stdout:
                if not got.is_set():
                    ready.append(line)
                    got.set()
                elif line.strip() == '{"stopping": true}':
                    self.stopping.set()
                else:  # anything else printed later goes to the log, so the pipe never fills up
                    _log(self.log_path, "backend: " + line.rstrip())
            got.set()

        threading.Thread(target=read, daemon=True, name="litforge-backend-out").start()
        got.wait(timeout)
        try:
            url = json.loads(ready[0])["url"] if ready else ""
        except (ValueError, KeyError, TypeError):
            url = ""
        if url:
            return str(url)
        if self.proc.poll() is None:
            reason = f"it did not answer within {timeout:g} s"
            self.proc.kill()
        else:
            reason = f"it exited with code {self.proc.returncode}"
        self.proc.wait()
        detail = _tail(self.log_path, since)
        raise BackendError(f"LitForge did not start: {reason}." + (f"\n\n{detail}" if detail else ""))

    def stop(self, timeout: float = 15.0) -> None:
        """Close its stdin and wait; it stops its jobs and owned processes itself. Killed only if it hangs."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            assert proc.stdin is not None
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            _log(self.log_path, f"the backend did not stop within {timeout:g} s; terminating it")
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def error_html(message: str, log_path: Path) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>body{font:15px system-ui,sans-serif;"
        "margin:40px;color:#222;background:#fafaf8}pre{white-space:pre-wrap;background:#f0eee9;padding:12px;"
        "border-radius:6px;max-height:50vh;overflow:auto}@media (prefers-color-scheme:dark){body{color:#ddd;"
        "background:#1c1c1e}pre{background:#2a2a2d}}</style></head><body><h2>LitForge could not start</h2>"
        f"<pre>{html.escape(message)}</pre><p>The full log is in <code>{html.escape(str(log_path))}</code>. "
        "Close this window and open LitForge again; if it fails again, send that file with your report.</p>"
        "</body></html>"
    )


def show_native_error(title: str, text: str) -> None:
    """Shown when there is no window to show it in (no window library, or the engine failed to start)."""
    sys.stderr.write(f"{title}: {text}\n")
    try:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            script = f"display alert {json.dumps(title)} message {json.dumps(text)} as critical"
            subprocess.run(["osascript", "-e", script], timeout=600, check=False)
        else:
            for cmd in (["zenity", "--error", "--title", title, "--text", text],
                        ["kdialog", "--title", title, "--error", text],
                        ["notify-send", "-u", "critical", title, text]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, timeout=600, check=False)
                    break
    except Exception:  # noqa: BLE001 - the message is already on stderr and in the log
        pass


def run_desktop(backend_cmd: Sequence[str], *, gui: str, title: str, log_path: Path,
                icon: Path | None = None, storage_path: Path | None = None,
                preload: Sequence[str] = (), workspace: Path | None = None, runtime_dir: Path | None = None) -> int:
    """Open the window and run until it is closed: 0 after a normal session, 1 when the backend failed (its
    error is shown in the window), 2 when no window could be opened (a native message is shown instead).

    With `workspace`, one window per library: when one is already open it is brought forward instead."""
    log_path = Path(log_path)
    holder: dict[str, object] = {}

    def bring_forward() -> bool:
        window = holder.get("window")
        if holder.get("closed"):
            return False  # closed, still stopping its backend: the next launch waits for the library
        if window is not None:
            window.restore()  # type: ignore[attr-defined]
            window.show()  # type: ignore[attr-defined]
        return True

    lease = None
    if workspace is not None:
        try:
            lease = instance.acquire_instance(workspace, bring_forward, runtime_dir=runtime_dir)
        except instance.InstanceError as exc:
            _log(log_path, str(exc))
            show_native_error(title, str(exc))
            return 2
        if lease is None:
            _log(log_path, "LitForge is already open for this library: brought its window forward")
            return 0
    try:
        return _run_window(backend_cmd, gui=gui, title=title, log_path=log_path, icon=icon,
                           storage_path=storage_path, preload=preload, holder=holder)
    finally:
        if lease is not None:
            lease.close()


def _run_window(backend_cmd: Sequence[str], *, gui: str, title: str, log_path: Path, icon: Path | None,
                storage_path: Path | None, preload: Sequence[str], holder: dict[str, object]) -> int:
    for lib in preload:  # e.g. libxcb-cursor for Qt on X11, copied by the installer without sudo
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            _log(log_path, f"could not preload {lib}: {exc}")
    try:
        import webview
    except ImportError as exc:
        _log(log_path, f"the window library pywebview could not be loaded: {exc}")
        show_native_error(title, f"LitForge could not open its window: {exc}\n\nDetails are in {log_path}")
        return 2

    backend = Backend(backend_cmd, log_path)
    result = {"rc": 0}
    window = webview.create_window(title, html=LOADING_HTML, width=1280, height=860, min_size=(820, 560))
    assert window is not None
    holder["window"] = window
    coordinator = close.CloseCoordinator(window, backend.finishing, log=lambda text: _log(log_path, text))
    window.events.closing += coordinator.on_closing

    def session() -> None:
        try:
            url = backend.start()
        except BackendError as exc:
            _log(log_path, str(exc))
            result["rc"] = 1
            if not window.events.closed.is_set():
                window.load_html(error_html(str(exc), log_path))
            return
        if window.events.closed.is_set():
            return
        window.load_url(url)
        while not window.events.closed.wait(0.5):
            if backend.finishing():  # Quit in the page, or the close button: the backend is stopping
                window.destroy()
                return

    try:
        webview.start(session, (), gui=gui, private_mode=False,  # type: ignore[arg-type]  # gui is checked by pywebview
                      storage_path=str(storage_path) if storage_path is not None else None,
                      icon=str(icon) if icon is not None else None)
    except Exception as exc:  # noqa: BLE001 - any engine failure ends in the same message
        _log(log_path, f"the {gui} window engine failed: {exc!r}")
        show_native_error(title, f"LitForge could not open its window ({gui}): {exc}\n\nDetails are in {log_path}")
        result["rc"] = 2
    finally:
        holder["closed"] = True
        backend.stop()
    return result["rc"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LitForge desktop window host")
    parser.add_argument("--gui", required=True)
    parser.add_argument("--title", default="LitForge")
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--preload", action="append", default=[])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("backend", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    backend = args.backend[1:] if args.backend[:1] == ["--"] else args.backend
    if not backend:
        parser.error("give the backend command after --")
    return run_desktop(backend, gui=args.gui, title=args.title, log_path=args.log, icon=args.icon,
                       storage_path=args.storage, preload=args.preload, workspace=args.workspace)


if __name__ == "__main__":
    sys.exit(main())
