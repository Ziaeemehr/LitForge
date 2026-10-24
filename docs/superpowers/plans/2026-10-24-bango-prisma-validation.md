# Task 1 (final status and PRISMA 2020) — validation record, 2026-10-24

Plan: [Review reporting, Task 1](2026-10-23-bango-ideas.md#task-1-final-status-and-the-prisma-2020-diagram).
Branch: `feat/bango-prisma`. Codex (`codex exec --sandbox workspace-write`) wrote the code; Claude reviewed each
round, ran every check outside the sandbox and checked the real app.

Baseline before any change (`main` at `55b75e4`): full pytest green, Ruff and mypy clean, 262 UI tests passed.

## Semantics fixed beyond the plan text

- `final_status` keys are the primary papers only. A human mark wins; it sits at S2 when the paper has an S2 row,
  otherwise at S1. A model exclusion's reason is the first violated exclusion criterion with a verified quote.
- A PRISMA record is a distinct (source key, paper) pair among current project members. Database searches are
  keyed by source, all imports by `import`; citation, similar and link runs go to "other methods" (`citations`,
  `similar`, `links`). A member with no hit is counted under its `project_papers.source`, so every paper is
  identified. `duplicates_removed` = records − primary papers, so other versions count as duplicates.
- Sought = screened minus final S1 exclusions. Assessed = sought with an S2 row or a human mark at S2.
  Not retrieved = sought, not assessed, with no current parse run; the rest are awaiting full text.
- `litforge prisma --json` prints counts only; with `-o` it also writes the SVG.

## Round 1 — run, inspect, diagnose

Codex implemented Task 1 test-first; the new tests failed first with import errors. Checks outside the sandbox: full
pytest green (3 environment skips: live LLM, PowerShell, bwrap), 264 UI tests, Ruff, mypy. The four socket
tests that Codex could not run in its sandbox passed here. Real run: `litforge prisma -p default --json` on the user's
library gave 65 records, 6 duplicates and 59 papers, matching the database (59 project papers, no screening).

Defects found by review:
1. The Review report panel was built in JavaScript inside the Recents view (`#view-recents`) and re-mounted from
   `renderSideProjects()`, away from the screening work it reports on.
2. The new Python files used single-quoted strings; the codebase uses double quotes.

## Round 2 — fix and rerun

Codex moved the panel to static markup right after the AI screening panel in the results view, wired once at startup
with a remembered open state, reloaded on project change and render, with a one-line summary and one line per stage.
Quotes were converted. UI tests were updated, including a check that `#review-report` follows `#screen-panel`.
Checks: focused pytest green, 267 UI tests, Ruff, mypy.

Defects found by review:
1. All panel CSS is scoped to `#screen-panel`, so the new panel had no summary, chevron or body styling.
2. Two inline `style` attributes, which `index.html` otherwise never uses.
3. In the dark theme the SVG's labels outside the boxes would be black on the dark card.

## Round 3 — fix and rerun

Codex extended the screen-panel selectors to `#review-report`, moved the styles into `app.css` and gave the diagram
a fixed light paper background that scales to the card width. Real-app check: the developer server ran against a
copy of the library with sample screening rows, and headless Chrome screenshots were taken in light and dark themes.
The panel shows 65 records, 6 duplicates, 47 screened, 22 excluded at S1, 12 awaiting, 25 sought, 24 not retrieved,
1 awaiting full text, 20 included and 5 uncertain. These match the seeded rows. The diagram is readable in both themes.
Final checks outside the sandbox are recorded in the commit message.

## Not covered

- No real model run: Task 1 makes no model call.
- No native desktop window check of this panel; it uses only DOM features already exercised by the desktop smoke.
