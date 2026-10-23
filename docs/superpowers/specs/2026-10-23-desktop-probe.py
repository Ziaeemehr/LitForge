"""Disposable desktop-window probe for LitForge (desktop plan, Task 1). Not product code.

Evidence for docs/superpowers/specs/2026-10-23-desktop-compatibility.md.

Usage: python probe.py --gui qt|gtk|edgechromium|cocoa --litforge PATH_TO_litforge --data DIR [--preload LIB]
Opens a window with an inline loading page at once, starts `litforge ui` on a free port, navigates when
/api/info answers, runs feature checks in the page, prints one JSON report and closes.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

T0 = time.monotonic()
ap = argparse.ArgumentParser()
ap.add_argument("--gui", required=True)
ap.add_argument("--litforge", required=True)
ap.add_argument("--data", required=True)
ap.add_argument("--preload", default="")
ap.add_argument("--hold", type=float, default=3.0)
args = ap.parse_args()
if args.preload:
    ctypes.CDLL(args.preload, mode=ctypes.RTLD_GLOBAL)

import webview  # noqa: E402

# one-page PDF with the text "LitForge probe page"
PDF_B64 = (
    "JVBERi0xLjcKJcK1wrYKJSBXcml0dGVuIGJ5IE11UERGIDEuMjguMgoKMSAwIG9iago8PC9UeXBlL0NhdGFsb2cvUGFnZXMgMiAw"
    "IFIvSW5mbzw8L1Byb2R1Y2VyKE11UERGIDEuMjguMik+Pj4+CmVuZG9iagoKMiAwIG9iago8PC9UeXBlL1BhZ2VzL0NvdW50IDEv"
    "S2lkc1s0IDAgUl0+PgplbmRvYmoKCjMgMCBvYmoKPDwvRm9udDw8L2hlbHYgNSAwIFI+Pj4+CmVuZG9iagoKNCAwIG9iago8PC9U"
    "eXBlL1BhZ2UvTWVkaWFCb3hbMCAwIDU5NSA4NDJdL1JvdGF0ZSAwL1Jlc291cmNlcyAzIDAgUi9QYXJlbnQgMiAwIFIvQ29udGVu"
    "dHNbNiAwIFJdPj4KZW5kb2JqCgo1IDAgb2JqCjw8L1R5cGUvRm9udC9TdWJ0eXBlL1R5cGUxL0Jhc2VGb250L0hlbHZldGljYS9F"
    "bmNvZGluZy9XaW5BbnNpRW5jb2Rpbmc+PgplbmRvYmoKCjYgMCBvYmoKPDwvTGVuZ3RoIDg1L0ZpbHRlci9GbGF0ZURlY29kZT4+"
    "CnN0cmVhbQp42uMq5HIK4TJUMABCQwVzIwVzcwOFkFwu/YzUnDIFQ0OFkDSFaBuTZDNLcxMTM7M0cyMzczNTIwNzAyArzcwIwjYz"
    "BInaxYZ4cbmGcAVyAQAyQhMNCmVuZHN0cmVhbQplbmRvYmoKCnhyZWYKMCA3CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDA0"
    "MiAwMDAwMCBuIAowMDAwMDAwMTIwIDAwMDAwIG4gCjAwMDAwMDAxNzIgMDAwMDAgbiAKMDAwMDAwMDIxMyAwMDAwMCBuIAowMDAw"
    "MDAwMzIwIDAwMDAwIG4gCjAwMDAwMDA0MDkgMDAwMDAgbiAKCnRyYWlsZXIKPDwvU2l6ZSA3L1Jvb3QgMSAwIFIvSURbPEMyOEVD"
    "Mjk3NUZDMjhCNzlDMkIxQzI4NkMzOTE2QkMzPjxDNEY3M0RBRjFBMTg2NjQwMEI0NERGM0ZCQkQ4RDdCMT5dPj4Kc3RhcnR4cmVm"
    "CjU2MgolJUVPRgo="
)
LOADING = ("<html><body style='font:16px sans-serif;display:grid;place-items:center;height:90vh'>"
           "Starting LitForge…</body></html>")
CHECKS = r"""
window.__probe = null;
(async () => {
  const r = {};
  r.userAgent = navigator.userAgent;
  r.withResolvers = typeof Promise.withResolvers === 'function';
  r.structuredClone = typeof structuredClone === 'function';
  r.randomUUID = !!(self.crypto && crypto.randomUUID);
  r.segmenter = typeof Intl.Segmenter === 'function';
  r.cssHas = CSS.supports('selector(:has(a))');
  r.clipboardApi = !!(navigator.clipboard && navigator.clipboard.writeText);
  r.secureContext = window.isSecureContext;
  r.codemirror = typeof window.LitForgeCM;
  r.title = document.title;
  r.appNodes = document.querySelectorAll('button').length;
  try {
    const w = new Worker('/static/vendor/spell.worker.js');
    r.worker = await new Promise(res => {
      w.onerror = e => res('error: ' + (e.message || 'load failed'));
      setTimeout(() => res('ok'), 1500);
    });
    w.terminate();
  } catch (e) { r.worker = 'throw: ' + e; }
  try {
    const lib = window.pdfjsLib || await new Promise(res => {
      addEventListener('pdfjs-ready', () => res(window.pdfjsLib));
      setTimeout(() => res(null), 5000);
    });
    if (!lib) throw new Error('pdfjsLib missing');
    const bytes = Uint8Array.from(atob('%PDF%'), c => c.charCodeAt(0));
    const doc = await lib.getDocument({data: bytes}).promise;
    const page = await doc.getPage(1);
    const vp = page.getViewport({scale: 1});
    const c = document.createElement('canvas'); c.width = vp.width; c.height = vp.height;
    await page.render({canvasContext: c.getContext('2d'), viewport: vp}).promise;
    const text = (await page.getTextContent()).items.map(i => i.str).join(' ');
    r.pdf = 'ok pages=' + doc.numPages + ' text=' + JSON.stringify(text);
  } catch (e) { r.pdf = 'error: ' + e; }
  const div = document.createElement('div'); div.dir = 'auto'; div.textContent = 'سلام دنیا LitForge';
  document.body.appendChild(div); r.rtl = getComputedStyle(div).direction; div.remove();
  const nav = performance.getEntriesByType('navigation')[0];
  r.domContentLoadedMs = nav ? Math.round(nav.domContentLoadedEventEnd) : null;
  r.slow = performance.getEntriesByType('resource').sort((a, b) => b.duration - a.duration).slice(0, 5)
    .map(e => [e.name.replace(location.origin, ''), Math.round(e.startTime), Math.round(e.duration)]);
  window.__probe = r;
})();
""".replace("%PDF%", PDF_B64)

report: dict[str, object] = {"gui": args.gui, "python": sys.version.split()[0], "platform": sys.platform}


def tree_rss_kb(root: int) -> int:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            except OSError:
                continue
            kids.setdefault(ppid, []).append(int(d))
    total, stack = 0, [root]
    while stack:
        pid = stack.pop()
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                total += sum(int(line.split()[1]) for line in f if line.startswith("Pss:"))
        except OSError:
            pass
        stack.extend(kids.get(pid, []))
    return total


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(window: webview.Window) -> None:
    server = None
    try:
        port = free_port()
        log = open(os.environ.get("PROBE_SERVER_LOG", os.devnull), "w")  # noqa: SIM115 (lives as long as the server)
        server = subprocess.Popen([args.litforge, "--data-dir", args.data, "ui", "--no-browser", "--port", str(port)],
                                  stdout=subprocess.DEVNULL, stderr=log)
        url = f"http://127.0.0.1:{port}/"
        for _ in range(300):
            try:
                urllib.request.urlopen(url + "api/info", timeout=1).read()
                break
            except OSError:
                time.sleep(0.05)
        report["server_ready_s"] = round(time.monotonic() - T0, 2)
        window.load_url(url)
        window.events.loaded.wait(30)
        report["app_loaded_s"] = round(time.monotonic() - T0, 2)
        time.sleep(1.0)
        window.evaluate_js(CHECKS)
        for _ in range(100):
            res = window.evaluate_js("JSON.stringify(window.__probe)")
            if res and res != "null":
                report["checks"] = json.loads(res)
                break
            time.sleep(0.1)
        time.sleep(args.hold)
        if sys.platform != "linux":  # memory is read from /proc; use Task Manager / Activity Monitor elsewhere
            return
        report["window_tree_pss_mb"] = round(tree_rss_kb(os.getpid()) / 1024 - tree_rss_kb(server.pid) / 1024)
        report["server_pss_mb"] = round(tree_rss_kb(server.pid) / 1024)
    except Exception as exc:  # report, never hang
        report["error"] = repr(exc)
    finally:
        if server:
            server.terminate()
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                server.kill()
        window.destroy()


window = webview.create_window("LitForge probe", html=LOADING, width=1200, height=800)
window.events.shown += lambda: report.setdefault("window_shown_s", round(time.monotonic() - T0, 2))
webview.start(run, window, gui=args.gui)
print(json.dumps(report, ensure_ascii=False, indent=1))
