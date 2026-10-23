# Standalone Desktop Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This document is a proposal for review, not authorization to start implementation.

**Goal:** Launch LitForge in its own desktop window on macOS, Windows and Linux, without opening Chrome, Firefox or another browser application.

**Architecture:** Keep the existing Python backend, HTTP API, HTML/JavaScript interface, CodeMirror editor and PDF.js viewer. Add a small pywebview desktop host process that owns the window and starts the backend as a child process (`litforge desktop-server`) on a loopback port. The host imports only the standard library and pywebview, so on Linux it can run under the system Python with the system's WebKitGTK; the backend keeps its own uv Python 3.12. Preserve a normal, writable Python environment for optional tools; treat native installers as a subsequent distribution milestone rather than freezing the entire backend immediately.

**Tech Stack:** Python 3.12 (backend), pywebview 6.2.1 with a per-OS engine: WebView2 on Windows, WKWebView on macOS, and on Linux the system WebKitGTK with a PyQt6 QtWebEngine fallback. Measured in Task 1 ([compatibility report](../specs/2026-10-23-desktop-compatibility.md)). existing stdlib HTTP server, vanilla JavaScript, uv, pytest and node:test. Native platform build tools are introduced only in the distribution milestone.

**Spec:** The embedded design below records the user's requirement and the proposed technical decisions. No separate design has been approved. Confirm the renderer choice after the compatibility investigation and review this plan before product implementation.

## Execution checkpoint (2026-10-23)

The user requested resuming this plan, three rounds of execution/diagnosis/fixes, and GitHub Actions
validation on Linux, Windows and macOS. Commits `24e9974` through `02b15a2` implement the server,
host, workspace selection, instance ownership, close flow, links/downloads, engine setup and launchers
(Tasks 2–6). Their unchecked acceptance items below are still requirements, not proof of completion.
Task 1 has Linux X11 evidence and a macOS 27 Apple Silicon probe; the proposed minimum OS versions
and remaining manual interactions are unverified.

**Validation scope:** finish automated cross-platform validation of Tasks 1–6. Bring forward only Task 8's
test matrix and real-webview evidence collection. Task 7's native packaging and Task 8's artifact
build/sign/publish steps remain later work, after Milestone B is validated and used daily.

Use `.github/workflows/desktop.yml` for a native matrix (Ubuntu GTK, Windows WebView2, macOS Cocoa),
`scripts/desktop_smoke.py` for a bounded production-host smoke check with scratch data, and
`docs/desktop-release-checklist.md` for evidence from all three rounds. Run the full Python suite,
Node UI suite, Ruff and mypy on Linux, desktop Python tests and UI tests on every target, and the
real engine on every target. A smoke failure must fail its job and preserve logs. Diagnose before
fixing, then rerun; a green rerun without code changes is a stability round. Never substitute three
identical retries of a known defect for a fix. Preserve the current branch and its in-progress changes.

**Checkpoint result:** the three rounds are recorded in the linked validation checklist. Round 3,
[Actions run 40016796125](https://github.com/Ziaeemehr/LitForge/actions/runs/40016796125), passed
all five jobs on commit `b5bcdcb`: native GTK, WebView2, Intel Cocoa and ARM Cocoa, plus the full
Linux suite (2129 passed, 4 skipped), 258 UI tests, Ruff and mypy. This completes Task 8's
automated validation subset only. **Next step:** finish the remaining Task 1/6 manual input,
download/dialog, existing-library, running-job close, minimum-OS and sleep/wake acceptance checks.
Then follow the plan's daily-use gate before Task 7's native runtime/installers. Do not mark all
Tasks 1–6 complete merely because their automated checks passed.

## Embedded design

### Confirmed requirement

The user wants a normal application window opened from the LitForge icon, without launching a browser application. The application must support macOS, Windows and Linux. The user does not require removing HTML, JavaScript or an embedded web engine, or rewriting the interface in native widgets. The current request is to write this plan only.

### Current implementation

- `src/litforge/ui/server.py:serve()` creates an `App` and `ThreadingHTTPServer`, calls `webbrowser.open()`, and blocks in `serve_forever()`.
- `stop_when_idle()` stops the browser-mode server after approximately 120 seconds without a heartbeat, provided no job is running. This is unsuitable for a minimized desktop window whose JavaScript timers may be throttled.
- `src/litforge/cli.py:launch()` currently delegates to `app(["ui"])`. `pyproject.toml` already defines the windowless Windows entry point `litforge-app`.
- `src/litforge/shortcut.py` creates Linux desktop entries, a macOS `.app` shell launcher and Windows shortcuts. They currently select the browser UI; a `.app` launcher is not yet a self-contained application bundle.
- `install.sh` and `install.ps1` install a source checkout and a uv environment. Data normally lives in the install directory's `data/`; shortcut working directories preserve this behavior.
- `src/litforge/tools.py` locates a source project and uv for optional dependency installation. A frozen Python executable would not be a transparent replacement for this workflow.
- `src/litforge/config.py` locates default configuration relative to the source package. Native distributions must explicitly include configuration, UI assets and writing templates, rather than assuming a repository checkout exists.
- There is currently no `.github/workflows/` directory. Release workflows in this plan are new infrastructure.

### Approaches and proposed choice

The user's priorities (2026-10-23) are: light and fast; a good working experience; installation without errors on every OS.

| Approach | Engine per OS | Extra download | Install risk | Verdict |
| --- | --- | --- | --- | --- |
| **pywebview, engine chosen per OS, host in its own process** | Windows WebView2 (Chromium, built into Windows 10/11); macOS WKWebView (system WebKit); Linux system WebKitGTK, with QtWebEngine (PyQt6) where the system lacks it | Windows and macOS: under 15 MB of Python packages. Linux GTK: none; it is part of every Ubuntu desktop through `update-manager`. Linux Qt fallback: about 210 MB | Low: no compiler and no sudo. The GTK host uses the system Python's own `gi`. The Qt fallback needs `libxcb-cursor0` on X11; Task 6 handles it | **Proposed, measured in Task 1** |
| pywebview with GTK inside the backend's uv Python | Linux WebKitGTK | Small | High: PyGObject is published only as a source package on PyPI, so it must be compiled. The system `gi` matches the system Python (3.10 on Ubuntu 22.04, 3.13 on Fedora 41), not our pinned 3.12 | Rejected: a separate host process removes the problem |
| pywebview with Qt as the only Linux engine | Chromium | About 210 MB (PyQt6), or about 260 MB (PySide6 6.12, whose WebEngine needs all of Addons) | Medium: on X11 Qt ≥ 6.5 aborts without `libxcb-cursor0`, which is missing on a default Ubuntu (reproduced) | Fallback only: about 520 MB on disk and about 170 MB more memory than GTK |
| pywebview with Qt on all three OSes | Chromium everywhere | About 210 MB on every OS | Medium (as above) | Fallback for macOS only if WKWebView has a blocker |
| Electron with a Python sidecar | Chromium everywhere | About 100 MB of Electron per OS, on top of the Python environment that the optional tools need anyway | Medium: a second toolchain (Node), a second updater, and sidecar process management | Rejected: the heaviest option, and it removes no Python packaging work |
| Tauri with a Python sidecar | The same system webviews, so WebKitGTK on Linux | Small shell plus a frozen Python | Medium–high: a Rust toolchain and a frozen-Python sidecar, which conflicts with the writable environment the optional tools need | Rejected: has the Linux webview problem without solving packaging |
| Chromium/Edge `--app` window | An installed browser | None | Low | Rejected: it is a browser application, which the requirement excludes; Linux and macOS may have no Chromium |
| Rebuild the UI with native widgets | — | — | — | Outside this requirement |

The proposal uses two engine families, WebKit (macOS, Linux) and Chromium (Windows, the Linux fallback); all page checks passed on both in Task 1. The same pywebview host code drives every OS; only the `gui=` argument and, on Linux GTK, the Python that runs the host differ. If an engine fails, the fallback is Qt for that OS, a one-line change with no new host. Electron is not a fallback, and the plan does not build two hosts.

Task 1 measured on Ubuntu 24.04:

- **GTK:** 256 MB window memory, no download.
- **Qt:** 360–430 MB window memory, about 210 MB download, about 520 MB on disk.

Windows and macOS still need their Task 1 runs. The user has not yet approved the decision.

### Proposed behavior

1. The icon launches `litforge-app`; a developer may use `litforge desktop`.
2. The desktop host resolves the workspace explicitly and opens one LitForge window at once, showing a built-in loading page (inline HTML, no server needed).
   - In parallel it starts the backend child process on `127.0.0.1` with an OS-assigned port, waits until the backend serves `/api/info`, then navigates the window to it.
   - Targets: a window visible within 1 second of the click and a usable app within 3 seconds, measured on an unlocked, visible screen. A locked screen throttles Chromium to about one frame per second.
   - Measured in Task 1: window visible in 0.2–0.3 s; app loaded in 0.8 s with Qt and 1.3 s with GTK; backend 73–88 MB.
3. All existing application views stay inside that window. User-clicked external article/documentation links may open the default browser; launching and using LitForge itself must not open one.
4. Closing the window and clicking **Quit** share one close flow. Pending editor changes must be saved first. With running jobs, offer cancel-close or stop-jobs-and-quit; never silently discard changes or terminate work.
5. Minimizing or suspending the window must not trigger the browser heartbeat shutdown policy.
6. A second launch for the same workspace focuses the first window. A different workspace may run its own window. Do not attach to an unrelated listener or use port 8765 as proof of ownership.
7. Startup failures are shown in a dialog and recorded in a writable log directory. There is no silent browser fallback. Browser mode remains explicitly available as `litforge ui`.
8. Existing libraries, settings, keys, tools and models remain available. Opening a desktop window must not silently create an empty library because its working directory changed.

## Global Constraints

- Python remains `>=3.12,<3.13`; HTML/JavaScript UI and existing API remain reusable in browser mode.
- Desktop launch must not call `webbrowser.open()` for its application URL.
- Desktop HTTP binds to `127.0.0.1`, port `0`; preserve existing Host and Origin checks.
- No privileged JavaScript-to-Python bridge for arbitrary pages. Never load an external top-level page into a window with native privileges.
- GUI event loop runs on the host process's main thread; HTTP serving runs in the backend child process.
- The host module (`src/litforge/desktop/host.py`) imports only the standard library and `webview`. It must run under any Python ≥ 3.9, because on Linux GTK it runs under the system Python, and it talks to the backend only over loopback HTTP and the child's stdout. A test imports it with LitForge's other packages unavailable.
- Data and optional dependencies live in writable user directories, never in signed bundles, Program Files or temporary extraction directories.
- No bundled LLM weights, CUDA stack or OCR models in the base installer. Download optional components through existing explicit setup flows.
- Preserve browser UI, CLI and source installation workflows. No database layout change is required for the desktop host.
- Initial proposed test matrix: Windows 11 x64; macOS 13+ Apple Silicon and Intel; Ubuntu 22.04/24.04 x64. Validate these floors against the selected pinned runtime in Task 1; these are proposed support bounds, not already verified compatibility. Other Linux distributions and ARM targets are follow-up scope.
- Freeze the selected dependency versions in `uv.lock` during implementation, after compatibility checks. No dependencies are installed as part of writing this plan.
- Automatic updating and app store submission are follow-up scope; manual updates must preserve data and installed extras.

## Review Focus

- Unicode/space-containing workspace paths and a changed working directory: reuse the correct library (Task 3).
- Repeated launches and stale ownership records: focus the right window without terminating another process (Task 4).
- Close during autosave, compilation or downloads: cancel-close preserves work; confirmed quit cleans up owned processes (Task 5).
- Minimized/suspended windows and failed renderer startup: no heartbeat-driven shutdown and no orphan server (Tasks 2 and 5).
- Clean machine missing browser, Python or WebView dependencies: installation provides prerequisites or explains precisely how to recover (Tasks 1 and 7).

## File and responsibility map

Paths below are proposed additions unless already present. Keep host-specific logic out of the large API handler and frontend file where possible.

| Files | Responsibility |
| --- | --- |
| `src/litforge/ui/server.py` | Extract an owned, non-blocking server lifecycle; preserve `serve()` wrapper. |
| `src/litforge/desktop/__init__.py`, `host.py` | Desktop orchestration and pywebview integration; lazy imports. |
| `src/litforge/desktop/workspace.py`, `instance.py` | Stable workspace selection and per-workspace ownership/focus. |
| `src/litforge/desktop/close.py` | Desktop close handshake and coordination with existing jobs. |
| `src/litforge/ui/static/desktop.js` | Narrow adapter for editor flush, close status, downloads and navigation. |
| `src/litforge/cli.py`, `src/litforge/shortcut.py`, `pyproject.toml`, `uv.lock` | Desktop command, entry point, launcher targets and desktop extra. |
| `install.sh`, `install.ps1` | Install desktop dependencies and choose desktop launchers. |
| `src/litforge/config.py`, `src/litforge/tools.py`, `src/litforge/updates.py` | Distribution-safe resources/runtime integration, only where needed. |
| `packaging/desktop/`, `scripts/build_desktop.py` | Platform installer definitions and reproducible staging. |
| `.github/workflows/desktop.yml` | Per-platform builds and artifact checks. |
| `tests/unit/test_desktop_*.py`, `tests/integration/test_desktop_server.py`, `tests/ui/desktop.test.cjs` | Focused regression tests; real renderer tests remain separately required. |
| `docs/install.md`, `README.md`, `docs/desktop-release-checklist.md` | User launch/update instructions and platform acceptance evidence. |

## Milestone A: Verify the embedded renderer

### Task 1: Compatibility investigation and renderer decision

**Files:** Create `docs/superpowers/specs/2026-10-23-desktop-compatibility.md` (the report) and `docs/superpowers/specs/2026-10-23-desktop-probe.py` (the disposable probe, kept so the Windows and macOS runs use the same checks). It is not product code.

**Interfaces:** Consumes the current app via `litforge ui --no-browser`; produces a recorded engine/version matrix and the engine decision per OS.

Status on 2026-10-23: the Linux runs are done; the Windows and macOS runs are pending. See the report.

- [x] Select and record the pywebview version: 6.2.1, which needs Python ≥ 3.8.
- [x] Run the probe on Linux on GTK (system WebKitGTK 2.52 through the system Python) and on Qt (PyQt6 6.11 and PySide6 6.12). Record download size, installed size, memory and timings. Reproduce the Qt failure without `libxcb-cursor0` and verify the no-sudo preload workaround.
- [ ] Run the probe on Windows 11 (`gui="edgechromium"`) and on macOS 13 and 14+ (`gui="cocoa"`), using the commands in the report. On macOS 13, the `withResolvers` and `pdf` results decide the floor (Safari 17.4+ WebKit) or a switch of macOS to Qt. Do not vendor a second PDF.js build.
- [ ] Linux remainder: run on a Wayland session, on a machine without `gir1.2-webkit2-4.1` (the Qt fallback end to end) and on one non-Ubuntu desktop (Fedora or KDE).
- [ ] With a person at the keyboard on an unlocked screen, check on each engine: CodeMirror completion, shortcuts and IME; Persian typing; file input; drag and drop; downloads; `confirm()` dialogs; external links. Include macOS Cmd shortcuts.
- [ ] Record a missing-engine and a renderer-startup failure and the user-visible recovery path for each.
- [ ] Commit the report updates. Pass condition: every required flow has evidence or a specific fix assigned to Task 6, and the user has approved the per-OS engine decision.

## Milestone B: Desktop window using the existing installation

### Task 2: Extract an owned server lifecycle

**Files:** Modify `src/litforge/ui/server.py`; create `tests/integration/test_desktop_server.py`; retain `tests/integration/test_ui_server.py` coverage.

**Interfaces:** Add `start_server(paths: Paths, config: Config, *, host: str = "127.0.0.1", port: int = 0, idle_shutdown: bool = False) -> ServerHandle`. `ServerHandle` exposes `url: str`, `app: App`, `is_running: bool`, `stop(timeout: float = 5.0) -> None`. `stop()` is idempotent and reports a timeout rather than pretending it finished. Keep `serve(paths, config, host, port, open_browser)` compatible.

- [ ] Add failing integration tests: endpoint readiness, ephemeral port, no browser call, stale heartbeat does not stop desktop mode, repeated stop, address released after stop, and startup/bind failure closes resources.
- [ ] Run `uv run pytest tests/integration/test_desktop_server.py -v`; confirm failures exercise the absent lifecycle API.
- [ ] Extract startup and cleanup without duplicating handlers. Start serving in a named thread; stop HTTP from another thread; call `App.shutdown()` and `server_close()` exactly once. Retain single-instance browser behavior and idle watchdog only in the existing `serve()` wrapper.
- [ ] Run `uv run pytest tests/integration/test_desktop_server.py tests/integration/test_ui_server.py -v`; require all tests to pass, including existing browser quit/heartbeat tests. Commit this deliverable.

### Task 3: Add desktop startup with stable workspace selection

**Files:** Create `src/litforge/desktop/__init__.py`, `host.py`, `workspace.py`, `tests/unit/test_desktop_host.py`, `tests/unit/test_desktop_workspace.py`; modify `src/litforge/cli.py`, `pyproject.toml`, `uv.lock`.

**Interfaces:**

- **Workspace:** `workspace.resolve_workspace(explicit: Path | None) -> Path` returns the workspace directory containing `data/`.
- **Backend command:** `litforge desktop-server --workspace PATH` starts the Task 2 server with `port=0` and `idle_shutdown=False`. It prints one JSON line `{"url": ..., "pid": ...}` to stdout when `/api/info` answers. It exits when stdin closes, so it dies with the host and leaves no orphan.
- **Host:** `host.run_desktop(backend_cmd: list[str], *, gui: str, title: str, icon: Path | None) -> int` lives in `src/litforge/desktop/host.py`. It imports only the standard library and `webview`. It opens the window with the loading page, spawns `backend_cmd`, reads the URL line and navigates.
- **`litforge desktop`:** `litforge desktop --workspace PATH` resolves the workspace and the engine, then calls `run_desktop` in the same Python on Windows, macOS and Linux Qt. For Linux GTK it re-executes the host file with the host environment's Python, which Task 6 records in the user config as `desktop.host_python`.
- **Entry points:** switch `litforge-app` to `litforge.desktop:main`. Retain `litforge ui` and the normal CLI commands without importing GUI packages.

- [ ] Add failing tests for startup order (window shows the loading page first and navigates only after `/api/info` answers; a server that fails to start replaces the loading page with the error and log path), correct URL, no browser fallback, startup exception cleanup, unavailable GUI dependencies and CLI imports without the desktop extra.
- [ ] Add workspace tests covering explicit path, remembered workspace, existing source-launch directory, new install default (`~/LitForge`), spaces/Persian characters and read-only directories. Precedence: explicit option, saved desktop workspace, recognized existing install workspace, then the new-install default. Persist the choice in an OS user configuration directory. If multiple known libraries exist, show a workspace chooser; never merge or copy them implicitly.
- [ ] Run `uv run pytest tests/unit/test_desktop_host.py tests/unit/test_desktop_workspace.py -v`; confirm expected failures.
- [ ] Implement the `desktop` extra and lazy pywebview adapter using Task 1's version/backend. Use `Paths(workspace / "data")` and existing configuration loading explicitly; do not change global CLI path semantics. Set native window title/icon; show startup errors with a log location.
- [ ] Run the two test files again and manually open an existing library through `uv run --extra desktop litforge desktop --workspace PATH`. Confirm familiar projects appear and no application URL opens in a browser. Commit.

### Task 4: Make repeated launches focus the existing window

**Files:** Create `src/litforge/desktop/instance.py`, `tests/unit/test_desktop_instance.py`; modify `src/litforge/desktop/host.py`.

**Interfaces:** `acquire_instance(workspace: Path, on_activate: Callable[[], None]) -> InstanceLease | None`; `None` means activation was delivered to the owner. `InstanceLease.close() -> None` releases ownership idempotently. The lease owns a private activation endpoint and an OS-released lock, scoped to the canonical workspace path.

- [ ] Add failing tests for concurrent launches, same/different workspaces, stale record after a crash, PID reuse and a forged activation request. Only an authenticated instance for the exact workspace may be activated; stale metadata alone cannot establish ownership.
- [ ] Run `uv run pytest tests/unit/test_desktop_instance.py -v`; confirm failures.
- [ ] Implement an OS-released file lock plus a user-private endpoint record with random activation token; record the selected cross-platform lock dependency in the desktop extra if needed. Do not put the token in URLs, normal logs or frontend JavaScript. Marshal focus/show calls through the selected GUI backend's supported mechanism.
- [ ] Run tests and real double-click activation on Windows, macOS and Linux. Verify a crash permits restart and different workspaces stay independent. Commit.

### Task 5: Coordinate window close, editor saves and running jobs

**Files:** Create `src/litforge/desktop/close.py`, `src/litforge/ui/static/desktop.js`, `tests/unit/test_desktop_close.py`, `tests/ui/desktop.test.cjs`; modify `host.py`, `src/litforge/ui/server.py`, `src/litforge/ui/static/index.html`, and the writing/quit portions of `app.js` only where necessary.

**Interfaces:** `CloseCoordinator.request_close() -> None` starts one close attempt; `cancel_close() -> None` restores editing; `finish_close() -> None` releases the owned server and instance lease. Expose the narrow frontend adapter `window.LitForgeDesktop.prepareClose() -> Promise<{ready: boolean, running: number, error: string | null}>`. It awaits pending editor saves, retains unsaved text on error and reports active jobs. Do not expose arbitrary Python execution to JavaScript.

- [ ] Add failing tests: idle close; save flush before shutdown; save conflict/error cancels close; running-job cancel leaves app usable; confirmed stop cancels owned jobs/subprocesses; repeated close requests are coalesced; API Quit closes the native window too; startup/renderer failure releases server and lease.
- [ ] Run `uv run pytest tests/unit/test_desktop_close.py -v` and `node --test tests/ui/desktop.test.cjs`; confirm failures.
- [ ] Implement the native closing-event veto and asynchronous preparation handshake. Stop new work during accepted shutdown, reuse existing job cancellation and `App.shutdown()`, and never stop a shared external Ollama service. If cleanup times out, keep ownership and report the failure instead of claiming a clean exit.
- [ ] Preserve browser Quit behavior; desktop Quit must not leave the window displaying a dead server. Coordinate the existing `/api/quit` handling with editor preparation rather than letting it bypass saves.
- [ ] Run the focused tests plus existing writing and UI-server regressions. Manually minimize for more than two minutes, sleep/wake the machine and close during compilation/download. Verify no lost edits and no owned server/process remains after a successful exit. Commit.

### Task 6: Integrate launchers and desktop interactions

**Files:** Modify `src/litforge/shortcut.py`, `install.sh`, `install.ps1`, `desktop.js`, `tests/unit/test_shortcut.py`, `tests/ui/desktop.test.cjs`, `docs/install.md`, `README.md`. Add desktop-specific tests to existing installer coverage where available.

**Interfaces:** Extend `create_shortcut(..., *, mode: Literal["browser", "desktop"] = "browser")` without changing existing callers. Desktop shortcuts pass an explicit workspace and invoke `litforge desktop` or the windowless `litforge-app` entry point. Installers request desktop mode after installing its dependencies.

- [ ] Add failing launcher tests for Windows windowless execution, macOS bundle script, Linux desktop-entry quoting, explicit workspace, and browser-mode compatibility. Add UI tests for external-link routing and pending-save failures from Task 5.
- [ ] Run `uv run pytest tests/unit/test_shortcut.py -v` and `node --test tests/ui/desktop.test.cjs`; confirm expected failures.
- [ ] Update installers to install `desktop` alongside existing `embed` and `documents` extras; preserve existing optional extras. Provide WebView2 prerequisite handling on Windows: it is part of Windows 11 and of updated Windows 10; otherwise point to the Evergreen bootstrapper. Check the system WebKit version on macOS. On Linux, the installer chooses the engine in this order:

1. **GTK:** if `/usr/bin/python3 -c "import gi; gi.require_version('WebKit2','4.1')"` (or `'4.0'`) succeeds, create the host environment with `uv venv --python /usr/bin/python3 --system-site-packages <install>/desktop-host` and install the pinned `pywebview` into it. No sudo and no compiler are needed.
2. **Qt:** otherwise install `PyQt6` and `PyQt6-WebEngine` (pinned) into the LitForge environment. On X11, when `libxcb-cursor.so.0` is missing, a Debian or Ubuntu system runs `apt-get download libxcb-cursor0` into `<install>/desktop-libs` without sudo, and the host preloads it with `ctypes.CDLL(..., RTLD_GLOBAL)`. On other distributions the installer prints the exact `dnf`/`zypper`/`pacman` line.

Record the engine, the host Python and any preloaded library in the user config. Settings shows them, and **Settings → Desktop engine → Check again** repeats the detection. Never silently use an obsolete renderer or fall back to a browser.
- [ ] Implement the Task 1 fixes for download/save dialogs and top-level navigation. Internal API/PDF routes stay in the app; clicked external HTTP(S) URLs use the OS browser without native privileges; reject unexpected executable URL schemes. An external page must never replace the privileged app document.
- [ ] Run launcher/UI tests and manual imports, PDF downloads, BibTeX/export, clipboard, Persian entry and Writing Studio compilation in the actual webviews. Update launch/quit docs and distinguish source-install updates from native-installer updates. Commit.

**Milestone B acceptance:** On all proposed supported OS targets, the existing installer creates an icon opening a LitForge window; normal app use never launches a browser; library/settings/extras persist; repeated launch and shutdown work. Python/runtime installation remains managed by the existing installer, without manual runtime setup by the user. This already satisfies the requested browser-free launch.

## Milestone C: Native installers and release validation

### Task 7: Package the runtime without breaking optional tools

**Files:** Create `packaging/desktop/`, `scripts/build_desktop.py`, `tests/unit/test_desktop_distribution.py`; modify `config.py`, `tools.py`, `updates.py` and `desktop/workspace.py` where distribution paths require it. Add platform-specific runtime manifests under `packaging/desktop/`.

**Interfaces:** `python scripts/build_desktop.py --target TARGET --output DIR` builds on the matching OS/architecture. Targets: `windows-x64`, `macos-arm64`, `macos-x64`, `linux-x64`. Produce platform payload manifests recording application version, Python/runtime versions, renderer prerequisites and checksums.

- [ ] First prove the packaging layout with one optional dependency: packaged app code/resources plus a managed, writable CPython 3.12 environment and uv under a per-user runtime directory. Preserve the source-project layout needed by `tools.py`, including its metadata and lockfile. Build a new environment at installation time; do not copy a non-relocatable development `.venv` into an installer.
- [ ] Add failing tests for resource lookup independent of CWD, read-only app bundles, writable tools/model/cache locations and interrupted runtime provisioning. Bundle/check configuration, templates, icons, PDF.js workers, dictionaries and editor assets. Ensure failed provisioning is retriable without touching library data.
- [ ] Run `uv run pytest tests/unit/test_desktop_distribution.py tests/unit/test_optional_tools.py -v`; confirm expected new failures, then implement layout/path adapters and pass the tests. Keep source-install discovery compatible.
- [ ] Produce Windows per-user setup executable, separate macOS `.app`/DMG artifacts for Intel and Apple Silicon, and a Linux `.deb` for the supported Ubuntu versions declaring its WebKitGTK runtime prerequisites. Record architecture-specific native dependencies and bundled-license notices. Broader Linux packaging is follow-up scope.
- [ ] On clean machines without Python/uv, verify installation, launch and reinstall. Bundle the managed runtime/base dependency payload or provision it visibly during setup with verified artifacts; document any network requirement before calling the installer self-contained. Heavy model/tool downloads remain separate.
- [ ] Verify a real supported optional tool install is detected after restart. Test a base update with existing extras; changing bundled Python into an executable must not silently disable optional features. Keep manual native updates separate from source `git`/`uv sync` instructions, using runtime version staging rather than modifying a running environment.
- [ ] Verify uninstall keeps user libraries/settings/models unless the user explicitly chooses data removal. Commit only after the clean-install and update paths pass.

### Task 8: Automate builds and collect release evidence

**Files:** Create `.github/workflows/desktop.yml`, `docs/desktop-release-checklist.md`; update `docs/install.md` and `README.md` with verified artifact names and actual support bounds.

**Interfaces:** CI produces versioned, architecture-labelled artifacts with checksums. Release publishing is a separate explicit operation; creating the workflow does not publish a release.

- [ ] Add an OS/architecture matrix using available native runners; build on each target rather than assuming cross-compilation. Run Python unit/integration checks and `node --test tests/ui/` before packaging.
- [ ] Run `uv run pytest`, `uv run ruff check .`, `uv run mypy` and `node --test tests/ui/`; resolve new failures and distinguish any pre-existing failures using a baseline before implementation.
- [ ] Exercise actual webviews through a visible desktop session or an appropriate Linux virtual display. Existing node DOM mocks are regression tests, not proof that WKWebView/WebView2/WebKitGTK works.
- [ ] Capture per-target evidence for clean install, existing workspace, repeated launch, minimization, close with dirty editor, running-job quit, PDF rendering/export, Persian input, optional tooling, manual update and uninstall. Record failing targets instead of labelling all three OS families supported prematurely.
- [ ] Add signing/notarization steps for macOS and signing for Windows when credentials are supplied via CI secrets. Test packaged artifacts after signing on clean machines; mark unsigned previews as previews. Certificate/account procurement and runner availability may add elapsed time.
- [ ] Finish the checklist and update user instructions with tested runtime/OS versions and remaining limitations. Commit workflow/docs. Publish artifacts only under a subsequent authorized release action.

## Estimates and dependency order

These are engineering estimates for one developer with access to all target systems, not guarantees. Totals are cumulative and include investigation/rework allowance.

| Deliverable | Expected elapsed development effort |
| --- | --- |
| Disposable window probe on one OS | 1–3 working days |
| Milestones A–B: usable desktop launch on all supported targets | Approximately 1–2 working weeks |
| Milestones A–C: tested native installers and release preparation | Approximately 3–5 working weeks |
| Broader OS/architecture support or automatic updates | Separate estimate; potentially 4–8 weeks total |

Task order: **1 → 2 → 3 → 4 → 5 → 6 → 7 → 8**. A renderer blocker in Task 1 or a runtime/optional-tool blocker in Task 7 changes the estimate and requires revising the affected tasks. Native installers are an additional milestone; do not delay the already useful browser-free source installation unnecessarily.

## Completion criteria

- The approved support matrix has real runtime evidence, including both Mac architectures if both are advertised.
- The app icon opens its own usable window with no external browser launch for the app URL.
- Existing library and settings remain present, and no normal write targets the application bundle.
- Editing, PDF preview, import/export and supported optional tools pass on actual webviews.
- Minimize/sleep do not shut down the app; repeated launch focuses the right workspace.
- Both Quit and window close preserve edits and handle running jobs consistently.
- Successful exit leaves no owned HTTP listener, lock or app-owned model/compiler process.
- Browser mode and CLI still pass their existing checks.
- Only completed milestones are described as shipped; automatic updates and unsupported platforms are not implied.

## Review notes (2026-10-23, revision 2)

A second, senior review took the user's priorities into account: light and fast, a good working experience, and installation without errors. These changes are already applied above:

1. **Engine choice (superseded by Task 1, 2026-10-23).** This revision first proposed Qt WebEngine through PySide6 for Linux. Task 1's measurements reversed that:
   - PySide6 6.12's WebEngine needs all of `PySide6-Addons` (about 260 MB download, 659 MB installed).
   - Qt fails on a default Ubuntu X11 desktop without `libxcb-cursor0`.
   - The system WebKitGTK is already on every Ubuntu desktop and uses 256 MB against 360–430 MB for Qt.

   The plan now uses GTK first, with the host running under the system Python in its own process, and PyQt6 as the fallback. See the approaches table and the [compatibility report](../specs/2026-10-23-desktop-compatibility.md).
2. **Fast startup.** The window appears immediately with a loading page instead of waiting for the server, with targets of 1 second to a visible window and 3 seconds to a usable app.
3. **Visible errors when the GUI library itself is missing.** `litforge-app` is a gui-script with no console. If importing pywebview fails, Task 3's dialog cannot be shown through pywebview. Task 3 adds a minimal per-OS fallback: `MessageBoxW` through ctypes on Windows, `osascript` on macOS, and `zenity` or `kdialog` on Linux when present. It shows the error and the log path. Task 3 adds a test that simulates the import failure.
4. **Guard against browser-only UI code.** `app.js` has 3 uses of `download=`, `.download =` or `window.open`. Task 6 routes them through `desktop.js` and adds a UI test that fails when a new browser-only download or `window.open` appears outside the adapter. [2026-10-23-bango-ideas.md](2026-10-23-bango-ideas.md) writes its files on the server for the same reason.
5. **Order with the features plan.** Run Task 1 (the probe, 1–3 days) first; it removes the main uncertainty cheaply. Then land Task 2 before the features plan changes `server.py`.
6. **Milestone C timing.** Start Milestone C only after Milestone B has been used daily for a few weeks. The one-line installer already provides Python (uv-managed) and uv, and Milestone C is most of the estimate and the risk.
7. **Licensing.** LitForge is AGPL-3.0. WebKitGTK (LGPL) is a system library and is not bundled. The Qt fallback uses PyQt6 (GPL-3.0, compatible with AGPL-3.0), which is smaller than PySide6. Record the bundled Qt notices in Task 7 if the fallback is ever bundled.

## Official references

- [pywebview overview](https://pywebview.flowrl.com/guide/)
- [pywebview installation and platform dependencies](https://pywebview.flowrl.com/guide/installation)
- [Electron overview, if the renderer investigation requires a fallback](https://www.electronjs.org/docs/latest)
- [Distribution and signing considerations](https://www.electronjs.org/docs/latest/tutorial/distribution-overview)
