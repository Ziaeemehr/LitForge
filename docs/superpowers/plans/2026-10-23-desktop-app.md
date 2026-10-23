# Standalone Desktop Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This document is a proposal for review, not authorization to start implementation.

**Goal:** Launch LitForge in its own desktop window on macOS, Windows and Linux, without opening Chrome, Firefox or another browser application.

**Architecture:** Keep the existing Python backend, HTTP API, HTML/JavaScript interface, CodeMirror editor and PDF.js viewer. Add a pywebview desktop host that owns a loopback server and the window lifecycle. Preserve a normal, writable Python environment for optional tools; treat native installers as a subsequent distribution milestone rather than freezing the entire backend immediately.

**Tech Stack:** Python 3.12, pywebview (proposed; validate in Task 1), existing stdlib HTTP server, vanilla JavaScript, uv, pytest and node:test. Native platform build tools are introduced only in the distribution milestone.

**Spec:** The embedded design below records the user's requirement and the proposed technical decisions. No separate design has been approved. Confirm the renderer choice after the compatibility investigation and review this plan before product implementation.

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

| Approach | Fit and tradeoff |
| --- | --- |
| pywebview with the existing Python server | Small conceptual change; uses platform web engines. Requires real tests for WebView2 on Windows, WKWebView on macOS and a selected Linux WebKitGTK backend. Proposed first choice. |
| Electron with a Python sidecar | Ships Chromium for a more consistent renderer. Larger distribution and an additional Node/Electron build and update surface. Fallback if the compatibility investigation finds a blocking webview issue. |
| Rebuild the UI with native widgets | Much larger rewrite of editing, PDF viewing and existing interactions. Outside this requirement. |

The user has not selected pywebview. It is the plan author's recommendation, subject to Task 1 evidence. Do not build both hosts. A fallback to Electron requires revising this plan's host and packaging tasks before continuing.

### Proposed behavior

1. The icon launches `litforge-app`; a developer may use `litforge desktop`.
2. The desktop host resolves the workspace explicitly, starts an owned server on `127.0.0.1` with an OS-assigned port, waits until it serves `/api/info`, then opens one LitForge window.
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
- GUI event loop runs on the main thread; HTTP serving and shutdown run outside it.
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

**Files:** Create `docs/superpowers/specs/2026-10-23-desktop-compatibility.md`. Use a disposable probe outside product source; do not commit a throwaway launcher as the application.

**Interfaces:** Consumes the current app via `litforge ui --no-browser`; produces a recorded backend/version matrix and a go/no-go decision for pywebview.

- [ ] Read current official pywebview installation, renderer, threading and download documentation. Select and record a pinned version compatible with Python 3.12.
- [ ] Run a small host probe on each supported OS: Windows WebView2, macOS WKWebView, Linux WebKitGTK. Record OS version, architecture and runtime versions.
- [ ] Exercise CodeMirror completion/shortcuts/IME, Persian RTL text, clipboard, local PDF.js rendering and workers, file input, drag/drop, downloads, confirmation dialogs and external links. Include macOS Cmd shortcuts and Linux Wayland/X11 where supported.
- [ ] Verify an app launch with browser opening disabled; test a missing WebView dependency and a renderer startup error. Record the user-visible recovery path.
- [ ] Record startup time, idle memory and size measurements as observations, not universal promises. Select pywebview if essential flows pass; otherwise document the blocker and revise this plan for Electron.
- [ ] Commit the compatibility report only. Pass condition: every required flow has evidence or a specific fix assigned to Task 6; no unresolved renderer blocker remains before Milestone B.

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

**Interfaces:** `workspace.resolve_workspace(explicit: Path | None) -> Path` returns the workspace directory containing `data/`. `host.run_desktop(workspace: Path) -> int` owns the server and main-thread GUI. Add `litforge desktop --workspace PATH`. Switch `litforge-app` to `litforge.desktop:main`; retain `litforge ui` and normal CLI commands without importing GUI packages.

- [ ] Add failing tests for startup order (server ready before window), correct URL, no browser fallback, startup exception cleanup, unavailable GUI dependencies and CLI imports without the desktop extra.
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
- [ ] Update installers to install `desktop` alongside existing `embed` and `documents` extras; preserve existing optional extras. Provide WebView2 prerequisite handling on Windows, system WebKit checks on macOS, and explicit WebKitGTK package guidance for the supported Linux distributions. Never silently use an obsolete renderer or fall back to a browser.
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

## Official references

- [pywebview overview](https://pywebview.flowrl.com/guide/)
- [pywebview installation and platform dependencies](https://pywebview.flowrl.com/guide/installation)
- [Electron overview, if the renderer investigation requires a fallback](https://www.electronjs.org/docs/latest)
- [Distribution and signing considerations](https://www.electronjs.org/docs/latest/tutorial/distribution-overview)
