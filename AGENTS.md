# Project instructions

<!-- codebase-memory-mcp:start -->
For structural codebase exploration, use the installed `codebase-memory` skill.
<!-- codebase-memory-mcp:end -->

## Execution and verification preference

The user requested this workflow on 2026-10-23 for future work in this project:

- Follow the relevant implementation plan and identify the next incomplete acceptance step before making changes.
- For an execution/validation task, perform three rounds of **run → inspect evidence → diagnose the root cause → fix actual defects → rerun**. Record each round and its results. If a round is already green, verify stability; do not invent defects or make unnecessary changes.
- For desktop changes, use GitHub Actions to validate Linux, Windows and macOS with their real webview engines where the runner permits it. Preserve logs, test results, platform versions and run links.
- Distinguish automated native-window checks from manual interaction, clean-machine installation and release/signing checks. Report unavailable infrastructure or access explicitly; never count an unexecuted round as completed.
- Keep future sessions informed through the desktop plan and its linked validation record. Three rounds are a verification procedure, not permission to publish releases, merge branches or remove user data.
