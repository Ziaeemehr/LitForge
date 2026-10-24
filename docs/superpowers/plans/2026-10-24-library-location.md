# Library location

## Goal
Choose a separate writable library workspace at install time, preserve existing installs and libraries, and share the choice across desktop launchers and CLI commands.

## Decisions
Use platform application-data directories for fresh code installs; preserve explicit, current-checkout and existing installs. Store the library workspace in desktop.json. Never move, copy or merge library data. CLI precedence: explicit data directory, environment, developer checkout data, saved workspace data, local data. Accept any writable workspace.

## File map
- `src/litforge/config.py`, CLI modules, `desktop/workspace.py`: shared resolution and library commands.
- `src/litforge/shortcut.py`: saved workspace launchers.
- `install.sh`, `install.ps1`: code discovery, library prompt/validation, persistence and summary.
- `tests/unit/`: isolated library, launcher, config and installer regression tests.
- `docs/install.md`, `README.md`: installation, changing libraries and uninstall guidance.

## Tasks
- [x] Add failing tests for library commands, path precedence, shortcuts and installers.
- [x] Implement Python resolution and commands; preserve existing shortcut behavior.
- [x] Implement both installers, preserving environment and terminal overrides.
- [x] Assess Settings: deferred; the export action requires a matching server route and UI wiring.
- [x] Update installation documentation.
- [x] Run three local verification rounds; record evidence and limitations below.

## Validation record (2026-10-24)

Codex implemented the change test-first; Claude reviewed it and ran the real installer.

- **Round 1:** review of config, CLI, shortcut and install.sh. The real `install.sh` ran in temporary HOME folders
  (real uv, git clone of this branch, real desktop-setup, real shortcut, real Tectonic download):
  - **Fresh install, interactive** (typed `~/MyLib`): code in `~/Library/Application Support/LitForge/app`, library
    in `~/MyLib`; desktop.json and the LitForge.app launcher both point to `~/MyLib`; `litforge library show`, run
    from another folder, resolves `~/MyLib/data`.
  - **Rerun** (non-interactive): updates the same program folder and keeps the library.
  - **Legacy `~/LitForge` checkout with data:** updated in place, library unchanged (marker file kept), launcher
    points to `~/LitForge`.
  - Finding: imports in the middle of `shortcut()`.
- **Round 2:** Codex moved the imports; no behaviour change.
- **Round 3:** full pytest 2159 passed, 3 skipped; 262 UI tests; Ruff; mypy; `sh -n install.sh`.
- **Not verified:** `install.ps1` on Windows (no PowerShell here, and the desktop CI job does not run the installer
  tests); Linux installer paths were not run on a real Linux machine. A Settings display of the library folder is
  deferred.
