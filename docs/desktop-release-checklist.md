# Desktop validation and release evidence

Plan: [2026-10-23-desktop-app.md](superpowers/plans/2026-10-23-desktop-app.md).
Renderer observations: [compatibility report](superpowers/specs/2026-10-23-desktop-compatibility.md).

## Current work

Validate Tasks 1–6 with native CI before Task 7's native installers. The user requested three
rounds of run, diagnosis, fixes and verification. Each round records the exact commit and run link;
a green stability round needs no artificial code change. A failed target remains failed until a
subsequent execution verifies its fix. A rerun without a fix is appropriate for an infrastructure
failure, not for a known deterministic defect.

`.github/workflows/desktop.yml` runs a full Linux regression suite plus native GTK, WebView2 and
Cocoa jobs, including both available Mac architectures. `scripts/desktop_smoke.py` opens the
production host against an empty temporary workspace with spaces and Persian characters, renders
a PDF, round-trips a real spell-worker message, checks essential JavaScript APIs and RTL direction,
activates the existing window, requests idle close, then checks backend exit and lease release.
A supervisor enforces a 120-second smoke deadline, then kills its process group (POSIX) or tree
(Windows), allowing up to 15 seconds for termination; any missing/failed check fails the job. JSON reports,
host/backend logs and JUnit test reports are uploaded even on failures.

The Windows runner is **Windows Server 2022**, not Windows 11. The Linux display is virtual X11,
not Wayland. Cocoa results apply to the actual runner version, not a claimed macOS 13 floor.
The native engine is real; mouse/keyboard dialogs and installation on end-user machines remain
separate acceptance checks. No installer or release artifact is produced by this workflow.

## Three-round record (2026-10-23)

| Round | Commit / run | Findings and changes | Result |
| --- | --- | --- | --- |
| 1 | `8b6b9da`; [run 40015300222](https://github.com/Ziaeemehr/LitForge/actions/runs/40015300222) | GTK and both Cocoa smoke checks passed. WebView2 passed page/PDF/worker/activation/close, but its closed-port check timed out at 2 s. Node 22 did not accept a test directory. Windows exposed POSIX-specific test assumptions; base-only mypy exposed two stale ignores. | Workflow failed; fixes prepared for round 2. |
| 2 | `cc9c01e`; [run 40015981059](https://github.com/Ziaeemehr/LitForge/actions/runs/40015981059) | Linux GTK and both Cocoa jobs passed. Windows desktop tests (134 passed, 3 skipped) and all UI tests passed; its smoke called JavaScript before the renderer loaded and hit pywebview's internal startup timeout. Full Linux suite exposed a PowerShell warning-stream assertion (2126 passed, 1 failed, 4 skipped). | Workflow failed; two fixes prepared for round 3. |
| 3 | Pending | Diagnose remaining findings and verify all targets again. | Not run |

### Round 1 diagnosis and fixes

- The Node command now enumerates `tests/ui/*.test.cjs`; the initial directory command worked with
  the newer local Node but failed to discover tests on CI's Node 22.
- GTK detection tests now create their fake system interpreter rather than depending on
  `/usr/bin/python3` existing on the machine running the test. The simulated Linux launch stubs
  uv lookup. Shortcut assertions parse the generated command and API tests compare path components,
  preserving verification on Windows without imposing POSIX separators.
- The desktop-server PID test accounts for Windows venv's Python redirector process. It retains
  direct PID equality on POSIX and still verifies an actual endpoint and successful child exit.
- The WebView2 backend had exited with code 0 and released its lease; only the subsequent connection
  probe timed out. Allow 10 s for Windows to return connection refusal, retaining failure on a live
  listener or a timeout. Round 2 must establish whether this resolves the observation.
- Nemotron's optional Transformers calls now use explicit dynamic casts instead of ignores that
  become unused when Transformers is absent. The CI errors were reproduced with
  `uv run --isolated --frozen mypy`; mypy then passed both with the local extras and without them.
- Quality steps now continue collecting independent test results after another check fails,
  so a type-check failure cannot hide regression-test findings.

Round 1 native evidence: Ubuntu 24.04 GTK had 136 passing desktop tests and one skip; both
macOS 15.7.9 architectures had 137 passing desktop tests. Windows Server 2022 had 119 passes,
15 failures and three skips. All three non-Windows smoke reports had no failed checks. The Windows
report's only failed check was backend shutdown's connection probe. These are hosted-runner results,
not claims of end-user OS installation coverage or representative startup performance.

### Round 2 diagnosis and fixes

- The Windows native window's `shown` event precedes completion of WebView2 initialization.
  `evaluate_js` independently waits only 20 seconds for the JavaScript bridge. The smoke driver
  now waits for the documented `loaded` event within its 60-second page deadline before evaluating
  the app, and tolerates navigation clearing that event. An uninitialized/broken renderer still
  fails. New tests observed RED→GREEN for waiting on load and enforcing the deadline.
- The complete Linux suite runs PowerShell, which is absent locally. The optional-installer test
  expected `Write-Warning` text in stderr alone; `pwsh` presents its Warning stream on stdout.
  The assertion now accepts the warning in either captured stream, while retaining the nonfatal
  exit and exact optional-install command checks. The installer behavior is unchanged.
- The smoke report now includes backend readiness timing and observed child exit codes to make
  backend startup/shutdown distinguishable from renderer initialization in subsequent evidence.

Before round 3, the clean local full suite passed 2127 tests (4 skipped), and the 24 smoke-driver
checks passed. Round 2's first-round portability failures did not recur. Its Windows renderer
timeout prevented rechecking the 10-second connection probe; that check is still required.

## Acceptance still required

- [ ] Native CI window flow on all listed targets, with links to passing evidence.
- [ ] Windows 11 and proposed minimum macOS versions on actual machines.
- [ ] Existing library and optional tools preserved through install/update.
- [ ] Icon launch, second launch and restart after a crash on each target.
- [ ] Dirty editor close, save failure/conflict and running-job cancel/confirmed quit in actual windows.
- [ ] Minimize for over two minutes and sleep/wake.
- [ ] Persian keyboard/IME, Cmd/Ctrl shortcuts, completion, clipboard, file input and drag/drop.
- [ ] Download/save dialogs, external links and PDF export through real user interactions.
- [ ] Linux Wayland and Linux Qt fallback end to end.
- [ ] Task 7 runtime packaging, clean installation, update and uninstall without losing data.
- [ ] Signing/notarization and release publication, when separately authorized and configured.
