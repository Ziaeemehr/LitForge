"""Bounded, real-webview check of the production desktop host; no user library is opened.

The supervisor may use the backend Python while --host-python selects Linux's GTK environment.
JSON evidence and logs survive renderer failures/timeouts. A missing or failed check fails the run.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BOOLEAN_CHECKS = ("desktop", "codemirror", "worker", "withResolvers", "structuredClone", "randomUUID",
                  "segmenter", "cssHas", "clipboardApi", "secureContext", "external_links")


def check_report(report: dict[str, Any]) -> list[str]:
    checks = report.get("checks") or {}
    failures = [name for name in BOOLEAN_CHECKS if checks.get(name) is not True]
    for name, expected in (("rtl", "rtl"), ("pdf_text", "LitForge probe page")):
        if checks.get(name) != expected:
            failures.append(name)
    for name, expected in (("activation", "activated"), ("host_exit_code", 0), ("window_closed", True),
                           ("backend_stopped", True), ("lease_released", True)):
        if name not in report or report[name] != expected:
            failures.append(name)
    if report.get("error"):
        failures.append("error")
    return failures


def sample_pdf() -> str:
    """Make a one-page PDF with a real xref, without importing backend PDF packages into the host."""
    stream = b"BT /F1 12 Tf 20 100 Td (LitForge probe page) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 150] "
               b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return base64.b64encode(data).decode("ascii")


PAGE_CHECKS = r"""
window.__desktopSmoke = null;
(async function () {
  const r = {desktop: !!window.LitForgeDesktop, codemirror: !!window.LitForgeCM,
    withResolvers: typeof Promise.withResolvers === 'function',
    structuredClone: typeof structuredClone === 'function', randomUUID: !!(crypto && crypto.randomUUID),
    segmenter: typeof Intl.Segmenter === 'function', cssHas: CSS.supports('selector(:has(a))'),
    clipboardApi: !!(navigator.clipboard && navigator.clipboard.writeText), secureContext: isSecureContext,
    userAgent: navigator.userAgent};
  try {
    const info = await fetch('/api/info').then(response => response.json());
    r.desktop = r.desktop && info.desktop === true;
    const worker = new Worker('/static/vendor/spell.worker.js');
    r.worker = await new Promise(resolve => {
      const timer = setTimeout(() => resolve(false), 5000);
      worker.onerror = () => { clearTimeout(timer); resolve(false); };
      worker.onmessage = event => {
        clearTimeout(timer);
        resolve(event.data.type === 'checked' && event.data.id === 42);
      };
      worker.postMessage({type: 'check', id: 42, words: []});
    });
    worker.terminate();
    const bytes = Uint8Array.from(atob('%PDF%'), c => c.charCodeAt(0));
    const doc = await pdfjsLib.getDocument({data: bytes}).promise;
    const page = await doc.getPage(1);
    const viewport = page.getViewport({scale: 1});
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width; canvas.height = viewport.height;
    await page.render({canvasContext: canvas.getContext('2d'), viewport: viewport}).promise;
    r.pdf_text = (await page.getTextContent()).items.map(item => item.str).join(' ');
    await doc.destroy();
    const div = document.createElement('div'); div.dir = 'auto'; div.textContent = 'سلام دنیا LitForge';
    document.body.appendChild(div); r.rtl = getComputedStyle(div).direction; div.remove();
  } catch (error) { r.error = String(error); }
  window.__desktopSmoke = r;
})();
"""


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def run_supervised(command: list[str], log_path: Path, *, timeout: float) -> str:
    """Own a process group/tree, including the backend; kill it on the hard smoke deadline."""
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, start_new_session=sys.platform != "win32",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)
        except OSError as exc:
            return str(exc)
        try:
            code = proc.wait(timeout=timeout)
            return f"Host exited with code {code}" if code else ""
        except subprocess.TimeoutExpired:
            error = f"Native smoke exceeded {timeout:g} seconds (process tree killed)"
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=log,
                                   stderr=subprocess.STDOUT, timeout=10, check=True)
                else:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError) as exc:
                error += f"; process-tree cleanup failed: {exc}"
                with contextlib.suppress(OSError):
                    proc.kill()
            proc.wait(timeout=5)
            return error


def wait_for_app(window: Any, *, timeout: float = 60) -> None:
    """Wait for the real load event before calling evaluate_js (which has its own 20 s deadline)."""
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        if not window.events.loaded.wait(min(remaining, .1)):
            continue
        try:
            if window.evaluate_js("Boolean(window.LitForgeDesktop && window.pdfjsLib)"):
                return
        except Exception:
            if window.events.loaded.is_set():
                raise  # renderer/bridge failure, rather than navigation clearing the load event
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"The desktop page did not initialize within {timeout:g} seconds")


def run_worker(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"platform": platform.platform(), "architecture": platform.machine(),
                              "host_python": sys.version, "gui": args.gui}
    started = time.monotonic()
    output = Path(args.output).resolve()
    report_path = output / "report.json"
    workspace = Path(args.workspace)
    runtime = workspace / "run"
    try:
        import webview

        opened_links: list[str] = []

        def open_external(url: str, *a: Any, **kw: Any) -> bool:
            opened_links.append(str(url))
            return True  # capture the OS-browser handoff without opening a browser or contacting a publisher

        webbrowser.open = open_external
        report["pywebview"] = importlib.metadata.version("pywebview")
        spec = importlib.util.spec_from_file_location("_litforge_smoke_host", ROOT / "src/litforge/desktop/host.py")
        assert spec and spec.loader
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        original_create = webview.create_window
        backends: list[Any] = []
        backend_type = host.Backend

        class ObservedBackend(backend_type):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                backends.append(self)

            def start(self, *a: Any, **kw: Any) -> str:
                url = super().start(*a, **kw)
                report["backend_ready_s"] = round(time.monotonic() - started, 3)
                return str(url)

        host.Backend = ObservedBackend

        def create_window(*a: Any, **kw: Any) -> Any:
            window = original_create(*a, **kw)

            def inspect_page() -> None:
                try:
                    wait_for_app(window)
                    report["app_ready_s"] = round(time.monotonic() - started, 3)
                    report["url"] = window.evaluate_js("location.href")
                    window.evaluate_js(PAGE_CHECKS.replace("%PDF%", sample_pdf()))
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        result = window.evaluate_js("JSON.stringify(window.__desktopSmoke)")
                        if result and result != "null":
                            report["checks"] = json.loads(result)
                            break
                        time.sleep(0.1)
                    else:
                        raise TimeoutError("Browser checks did not finish")
                    expected_links = ["https://doi.org/10.1234/litforge-smoke", "https://openalex.org/W123456789"]
                    for url in expected_links:
                        window.evaluate_js("""(() => {
                          const a = document.createElement('a'); a.href = %URL%;
                          a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'Paper link smoke';
                          document.body.appendChild(a); a.click(); a.remove();
                        })()""".replace("%URL%", json.dumps(url)))
                    deadline = time.monotonic() + 5
                    while len(opened_links) < len(expected_links) and time.monotonic() < deadline:
                        time.sleep(.05)
                    report["external_link_handoffs"] = opened_links
                    report["checks"]["external_links"] = (
                        sorted(opened_links) == sorted(expected_links)
                        and window.evaluate_js("location.href") == report["url"])
                    report["activation"] = host.instance.activate_existing(workspace, runtime_dir=runtime)
                except Exception:
                    report["error"] = traceback.format_exc()
                finally:
                    write_report(report_path, report)
                    window.destroy()  # production closing handler saves/quits; no fake JavaScript/native window

            def shown() -> None:
                report["window_shown_s"] = round(time.monotonic() - started, 3)
                threading.Thread(target=inspect_page, daemon=True).start()

            window.events.shown += shown
            window.events.closed += lambda: report.update(window_closed=True)
            return window

        webview.create_window = create_window
        report["host_exit_code"] = host.run_desktop(
            [args.backend_python, "-c", "from litforge.cli import app; app()", "--data-dir",
             str(workspace / "data"), "desktop-server"],
            gui=args.gui, title="LitForge desktop smoke", log_path=output / "backend.log",
            workspace=workspace, runtime_dir=runtime, storage_path=workspace / "webview")
        report["backend_stopped"] = bool(backends) and all(b.returncode == 0 for b in backends)
        report["backend_processes"] = [
            {"pid": b.proc.pid if b.proc is not None else None, "returncode": b.returncode} for b in backends]
        if report.get("url"):
            url = urlsplit(report["url"])
            try:
                # Windows may retry SYNs before reporting a refused connection to a closed local port.
                with socket.create_connection((url.hostname, url.port), timeout=10):
                    report["backend_stopped"] = False
            except ConnectionRefusedError:
                pass
            except OSError as exc:
                report["backend_stopped"] = False
                report["listener_error"] = str(exc)
        lease = host.instance.acquire_instance(workspace, lambda: None, runtime_dir=runtime, wait=1)
        report["lease_released"] = lease is not None
        if lease is not None:
            lease.close()
    except Exception:
        report["error"] = traceback.format_exc()
    report["failures"] = check_report(report)
    write_report(report_path, report)
    return int(bool(report["failures"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", required=True, choices=("cocoa", "gtk", "qt", "edgechromium"))
    parser.add_argument("--backend-python", default=sys.executable)
    parser.add_argument("--host-python", default=sys.executable)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return run_worker(args)
    report_path = output / "report.json"
    report_path.unlink(missing_ok=True)  # never accept a previous run's success
    with tempfile.TemporaryDirectory(prefix="LitForge فارسی desktop ", ignore_cleanup_errors=True) as workspace:
        command = [args.host_python, "-I", str(Path(__file__).resolve()), "--worker", "--gui", args.gui,
                   "--backend-python", str(Path(args.backend_python).absolute()), "--output", str(output),
                   "--workspace", workspace]
        error = run_supervised(command, output / "host.log", timeout=args.timeout)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        if error:
            report["error"] = error + ("\n" + str(report["error"]) if report.get("error") else "")
        report["failures"] = check_report(report)
        write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
