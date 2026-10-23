# Desktop window: compatibility investigation (desktop plan, Task 1)

> Date: 2026-10-23. Status: **Linux X11 and macOS 27.0.1 Apple Silicon probes run; Windows, Intel Macs and the proposed minimum macOS version still need real-machine runs** (steps below).
> Probe: [2026-10-23-desktop-probe.py](2026-10-23-desktop-probe.py), a disposable script, not product code.

## What the probe does

1. **Window first:** it opens a window at once with an inline "Starting LitForge…" page.
2. **Server:** it starts `litforge ui --no-browser` on a free loopback port with a scratch data folder.
3. **Navigation:** it navigates to the app when `/api/info` answers.
4. **Checks in the real page:**
   - PDF.js renders a one-page PDF and reads its text;
   - a Web Worker loads (`spell.worker.js`);
   - CodeMirror is present;
   - `Promise.withResolvers`, `structuredClone`, `crypto.randomUUID`, `Intl.Segmenter`, CSS `:has` and the clipboard API exist;
   - `dir="auto"` Persian text renders right to left.
5. **Memory:** it records timings and memory as PSS summed over the window's process tree, server excluded.

## Linux results (Ubuntu 24.04.5, X11, GNOME, NVIDIA RTX 5070, pywebview 6.2.1)

| | GTK: system WebKitGTK 2.52 | Qt: PyQt6 6.11 / QtWebEngine (Chromium 140) |
| --- | --- | --- |
| All page checks | pass | pass |
| Window visible | 0.23–0.33 s | 0.18 s |
| App loaded | 1.3 s | 0.8 s (offscreen) |
| Window memory (PSS) | **256 MB** | **360–430 MB** |
| Extra download | **none** (system packages) | about 210 MB of wheels (PyQt6) or about 260 MB (PySide6 6.12) |
| Installed size | none | 519 MB (PyQt6) or 659 MB (PySide6: QtWebEngine 6.12 pulls in all of `PySide6-Addons`) |
| Install without errors | yes where the system has `python3-gi` and `gir1.2-webkit2-4.1` (or `-4.0`) | **no** on X11 without `libxcb-cursor0`: Qt ≥ 6.5 aborts with "no Qt platform plugin could be initialized" (reproduced here) |

Server process (`litforge ui`): 73–88 MB, import 0.4 s, ready in 0.6–1.1 s. Every static file is served in 1–3 ms.

### Findings that change the plan

1. **The GTK path is already present on Ubuntu desktops.**
   - The dependency chain is `ubuntu-desktop-minimal` → `update-manager` → `ubuntu-release-upgrader-gtk`, which depends on `python3-gi` and `gir1.2-webkit2-4.1`. On 22.04 that package depends on `gir1.2-webkit2-4.0`, which pywebview also accepts.
   - So on a default Ubuntu desktop the light engine needs **no download and no sudo**.
   - The catch: `python3-gi` belongs to the *system* Python, while LitForge's backend is pinned to 3.12. The system Python is 3.10 on Ubuntu 22.04 and 3.13 on Fedora 41.
   - **Solution:** run the window host as its own small process under the system Python, in a separate environment (`uv venv --python /usr/bin/python3 --system-site-packages` plus `pywebview`; pywebview 6.2.1 needs Python ≥ 3.8). The backend keeps its own uv Python 3.12. This was verified here: the GTK row above ran exactly this way.
2. **Qt is the fallback, not the default, on Linux.** It works and loads fastest, but it costs about 210 MB of download, about 520 MB on disk and about 170 MB more memory. On X11 it also needs `libxcb-cursor0`, which a default Ubuntu does not have. Two ways around that missing library were checked:
   - **Preload without sudo:** download the distribution's own package and preload its library before Qt starts: `apt-get download libxcb-cursor0`, extract it, then `ctypes.CDLL(path, RTLD_GLOBAL)`. This was verified here: Qt then starts on `xcb`. It does not work for non-Debian distributions.
   - **One-line install:** otherwise print the exact install command.
   - **Choice between PyQt6 and PySide6:** prefer PyQt6. It is smaller, and GPL-3.0 is compatible with LitForge's AGPL-3.0.
3. **Two engine families, as planned.** WebKit is used on macOS and Linux GTK; Chromium on Windows and the Linux Qt fallback. All page checks pass on both families.
4. **A locked screen makes Chromium look slow.** With the screen locked, QtWebEngine loaded the page in about 9.7 s, in 1-second steps: Chromium drops to about one frame per second when nothing is displayed. Offscreen it loaded in 0.8 s, and headless Chrome loaded the same page in 0.1 s. This is not a LitForge problem. Measure startup only on an unlocked, visible screen.
5. **Separate host and server processes are simpler than one process.** The window appears before any LitForge import (0.2–0.3 s). The host's Python is independent of the backend's. A backend crash cannot take the window down silently, because the host shows the error. The plan's Tasks 2–5 are revised accordingly.

### Decision (proposed; needs the user's approval)

| OS | Engine | Host Python |
| --- | --- | --- |
| Windows | WebView2 (`gui="edgechromium"`) | LitForge's uv Python (pywebview pulls in pythonnet, small) |
| macOS | WKWebView (`gui="cocoa"`) | LitForge's uv Python (pyobjc, small) |
| Linux | WebKitGTK (`gui="gtk"`) when the system's `gi` and WebKit2 typelib import; otherwise Qt (`gui="qt"`, PyQt6) | GTK: system Python in a separate host environment. Qt: LitForge's uv Python |

The installer chooses the Linux engine once, by trying `python3 -c "import gi; gi.require_version('WebKit2','4.1')"` and then `'4.0'`. It records the choice, and Settings shows it. Electron, Tauri and the GTK backend inside the uv Python are rejected (see the plan's approaches table).

## macOS result (2026-10-23)

The existing disposable probe ran on macOS 27.0.1 (build 26A434), Apple Silicon (`arm64`),
CPython 3.12.15, pywebview 6.2.1 and PyObjC 12.2.2, using `gui="cocoa"` (WKWebView).
The window closed automatically after the checks; it used a scratch library in `/private/tmp`.

| Check | Result |
| --- | --- |
| Window shown | 2.69 s |
| Backend ready | 3.16 s |
| App loaded | 3.36 s |
| PDF.js rendering and text extraction | Pass: one page, `LitForge probe page` |
| Spell worker | No load error during the probe's 1.5 s observation; spellchecking itself was not exercised |
| CodeMirror | Present (`object`) |
| Promise.withResolvers, structuredClone, crypto.randomUUID, Intl.Segmenter, CSS :has | All present |
| Clipboard API / secure context | Present / true; clipboard interaction was not exercised |
| Persian DOM direction | `rtl`; keyboard input was not exercised |
| Navigation DOMContentLoaded | 158 ms |

This run exceeds the proposed 1 s visible-window and 3 s usable-app targets. These are single-run
measurements, not a startup benchmark. Memory was not measured. The probe launches the browser-mode
backend without opening a browser, so it does not validate the production desktop host's close,
single-instance or installer flows. Manual input, shortcuts, drag and drop, downloads and dialogs
remain unverified. This result establishes compatibility on this machine only; it does not establish
the proposed macOS 13 floor or Intel compatibility.

An additional automated smoke run used the production `host.run_desktop()` with the real Cocoa
engine and `litforge desktop-server`, in a separate scratch workspace. It waited for the page's
`window.LitForgeDesktop` adapter and 136 buttons, then requested window destruction. The host
returned 0, removed its instance JSON record and stopped its loopback listener (a subsequent
connection returned macOS `ECONNREFUSED`, errno 61). The reusable `.lock` file remained. This
checks an idle, unedited session only; dirty-editor closing, running jobs, repeated launch,
minimize/sleep and icon installation still need real-window checks.

Reproduce from the repository root with pywebview 6.2.1 installed in a separate probe environment:

```sh
<probe-python> -I docs/superpowers/specs/2026-10-23-desktop-probe.py \
  --gui cocoa --litforge "$PWD/.venv/bin/litforge" --data <empty-scratch-folder>
```

## Still to run (Windows and remaining macOS targets)

On each machine, with LitForge installed by the normal installer:

```
uv venv probe-env && uv pip install --python probe-env pywebview==6.2.1
probe-env/bin/python -I 2026-10-23-desktop-probe.py --gui edgechromium --litforge <path to litforge> --data <empty folder>   # Windows
probe-env/bin/python -I 2026-10-23-desktop-probe.py --gui cocoa --litforge <path to litforge> --data <empty folder>          # macOS
```

On Windows the paths are `probe-env\Scripts\python.exe` and `litforge.exe`. Record the JSON output and the window's memory from Task Manager or Activity Monitor (the probe reads memory only on Linux). On macOS 13 the result of `withResolvers` and `pdf` decides the macOS floor: Safari 17.4+ WebKit is needed. Also check by hand:

- IME and Persian typing in the Write editor;
- Cmd and Ctrl shortcuts;
- drag and drop of a PDF;
- an export download;
- a `confirm()` dialog;
- an external link.

## Not yet tested on Linux

- Wayland: the session here was X11.
- A machine without `gir1.2-webkit2-4.1`, to confirm the Qt fallback end to end.
- A Fedora or KDE desktop.
- IME and drag and drop, which need an unlocked screen and a person at the keyboard.

These belong to Task 1's remaining work and Task 6's manual checks.
