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

# Task 4 (RIS and BibTeX export with decisions)

Codex implemented `export/references.py` test-first. `bibtex_entry` gains `extra`, and a preprint keeps one `note`
field ("Preprint. LitForge: …"). Imports now decode `\textbraceleft{}`-style escapes and `\&`, so exports round-trip.
Records are sorted by title, then paper id. CLI `export-refs`, route `POST /api/review/references`, and RIS/BibTeX
buttons in the header export group, whose toast offers "Open export folder".

- **Round 1:** full pytest green (3 environment skips), 269 UI tests, Ruff and mypy. Real run on a library copy: 42 RIS
  records (included + excluded) and 20 BibTeX entries, both read back by `parse_ris`/`parse_bibtex` with titles and
  DOIs intact. Code review found no defect.
- **Round 2:** a real-browser screenshot showed the toast's folder button as an unreadable dark block, because `.toast`
  inverts colours and `.btn` does not. Codex added `.toast .btn` (transparent, inherited colour, currentColor border).
  Test-harness incident: the library copy kept the user's export folder, so one test file was written to
  `~/Downloads/LitForge/default/references.ris`. It was deleted at once (the folder again holds only `.DS_Store`),
  and the copy was pointed at a scratch folder.
- **Round 3:** screenshots confirm the button is readable in both themes. 269 UI tests, 449 focused Python tests
  (export, import, bib, writing, routes, CLI docs), Ruff and mypy green.
- **Not done:** importing the RIS file into Zotero (installed). That would write into the user's real Zotero library,
  so it is left as a manual acceptance check.

# Task 5 (Obsidian links in the Markdown export)

Codex added `export_markdown(obsidian=True)`: front matter `tags: [litforge/<status>]` (Task 1's final status) and
`aliases`, a `## Links in this export` section built from citations (`paper_relations`) and from papers sharing at
least 3 references (`paper_references.ref_id`), limited to exported notes, and `[[slug]]` links in README.
Flag `litforge export --obsidian` (plus `--json`), an "Obsidian links" checkbox in the header export group, and
the flag on all three server export paths. The fetch-then-export job now also honours "all versions", which it
used to ignore. A fixture keeps the default export byte-identical.

- **Round 1:** full pytest green, 271 UI tests, Ruff and mypy. Real export of the 59-paper library copy: 59 notes,
  11 with links, 93 `[[…]]` links in notes and README, 0 broken. Review finding: the link pass recomputed each
  note's slug with a copy of the loop's formula, so the two could drift apart and break links silently.
  (Harness note: the first export attempt failed because the copy lacked the parsed folder. It was not a code
  defect, and the code before this task failed the same way on that copy.)
- **Round 2:** Codex extracted `_note_title_and_slug()`, used by both passes. Byte-identical default export and
  the Obsidian tests passed.
- **Round 3:** full pytest 2173 passed, 3 skipped; 271 UI tests; Ruff; mypy. The real export again gave 59 notes and
  93 links, 0 broken.
- **Not done:** opening the folder as an Obsidian vault by hand (the Milestone A acceptance check).

# Task 3 (Search-string builder)

Codex added `analysis/search_strategy.py`. The model (task `search_strategy`, local by default) returns concepts
only. PubMed, Scopus, Web of Science, arXiv and OpenAlex strings are assembled deterministically from `SYNTAX` and
checked by `validate_query`. Results are cached in `analysis_runs`, keyed by criteria hash and database set. Adds the
CLI `litforge strategy`, `GET/POST /api/review/strategy`, and a "Search strings" card in the AI screening panel with
Copy and "Search with it" (at most 6 plain OpenAlex queries through the existing search).

- **Round 1:** full pytest 2186 passed, 3 skipped; 277 UI tests; Ruff; mypy. Real model run (qwen3:4b, sbi10
  criteria, 3 attempts): same concepts every time, all five strings valid. Review probes found three cleaner defects:
  apostrophes removed ("Parkinson's disease" → "Parkinson s disease"); any word before a colon deleted
  ("COVID-19: severe cases" → "severe cases"); and one concept with fewer than 3 usable synonyms aborting the build.
- **Round 2:** Codex fixed all three test-first. Apostrophes are kept and quoted, only known field-tag prefixes are
  removed, and a weak concept is dropped with a warning (fewer than 2 concepts still fails). Probes confirmed
  `validate_query` finds no problems.
- **Round 3:** a real-browser check with a real Build through the UI filled the PubMed, Scopus and OpenAlex strings.
  Defect: the databases showed internal ids ("pubmed", "wos") and a heavy default fieldset. Codex switched to proper
  names and compact styling. A reload showed the cached strings without a model call. Final: 2201 passed,
  3 skipped; 278 UI tests; Ruff; mypy.
- **Observation:** with qwen3:4b the concepts are usable but wordy ("neural network methods", "simulator calibration
  parameters"). Review the strings before using them in a database.
