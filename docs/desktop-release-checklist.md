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
| 1 | Pending | Establish native CI baseline after local smoke verification. | Not run |
| 2 | Pending | Diagnose round 1 findings, fix causes and rerun all targets. | Not run |
| 3 | Pending | Diagnose remaining findings and verify all targets again. | Not run |

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
