# Writing Studio v1 (essentials only) Implementation Plan

> **For agentic workers:** implement task by task, tests first. Each task ends green (pytest, ruff, mypy,
> `node --test tests/ui/`). The branch is merged only after three review rounds (review, fix, re-check), as for the
> other features.

**Goal:** A light LaTeX editor inside LitForge: write a document, cite papers from the library, compile to PDF
(English and Persian with `xepersian`), and use a language model for three actions on a selection. Nothing more in
v1 (see the "essentials only" table in the spec).

**Architecture:** Documents are plain folders under the project. A small `litforge.writing` package owns documents,
snapshots, compile, citation keys and the AI actions; the UI adds one view ("Write") using a vendored CodeMirror
bundle and the browser's own PDF viewer. Compilation runs as a background job with the existing job runner and
process-tree helpers. Model calls go through the existing `ModelGateway`.

**Tech stack:** Python 3.12, SQLite, the existing stdlib HTTP server and vanilla JS UI, CodeMirror 6 (pre-built,
vendored), Tectonic or system XeLaTeX, pytest, node:test.

**Spec:** docs/superpowers/specs/2026-10-18-latex-writing-studio-design.md ("First version: essentials only").

## Global constraints

- Local first. Editing, saving and compiling work offline once the engine (and its package cache) is installed.
- No model call without the user pressing an action; local model by default; cloud only with the usual approval and
  budget checks. Only the selection (and, for citations, the chosen evidence) is sent.
- A model suggestion is never applied without the user accepting it.
- Compile: no shell escape, working directory = the document folder, no writes outside it, time limit, the whole
  process tree is stopped on timeout or app shutdown.
- No path traversal: every file operation resolves inside the document folder (reuse the containment checks used for
  parse files and exports).
- Persian is a first-class case: right-to-left text in the editor, `xepersian` template compiles, AI actions keep
  LaTeX commands inside Persian text intact.
- Mutating HTTP endpoints are POST and origin-guarded; GET endpoints are read-only; UI text via `textContent`/`esc()`.

## Out of scope for v1

SyncTeX, PDF.js, outline, command autocomplete (only `\cite` search), spell-check dictionaries, word count, git
history UI, named versions, ZIP import/export, multiple engines per file, auto-recompile, assistant chat,
title/abstract, equation/table generators, completion while typing, MCP server.

## Review focus

- Compile sandbox: shell escape really off for both engines; files outside the folder cannot be read or written via
  `\input{/etc/...}`, `\openout` or symlinks; timeouts kill grandchildren.
- Snapshots and auto-save never lose text (crash during save, two tabs open on the same document).
- Citation keys stay stable once used; `references.bib` stays in sync and valid BibTeX (escaping of `{}`/`&`/`%`,
  non-ASCII names).
- AI guards: invented numbers and unknown `\cite` keys are flagged; LaTeX commands preserved; cloud approval cannot be
  bypassed from the editor.
- RTL: cursor movement, selection and diffs on mixed Persian/English lines.

---

### Task 0: Spike — Tectonic and `xepersian` (decides the Persian engine)

**Files:** scratch only; result recorded in the spec ("Risks and open questions") and in this plan.

**Steps:**
- Install Tectonic (latest release for macOS arm64) into a scratch folder; compile a minimal `xepersian` document
  with Vazirmatn (OFL) and a mixed Persian/English paragraph with `\cite`, a table and an equation.
- Measure first compile (package download size and time) and warm compile time; check offline recompile.
- Repeat with the system `xelatex` (MacTeX) if Tectonic fails.

**Decides:** default Persian engine (Tectonic if it compiles `xepersian` cleanly, otherwise system XeLaTeX), the font
to ship or download, and whether a bibliography tool beyond BibTeX is needed.

**Done when:** a written result with the exact commands, versions, sizes and timings.

**Result (2026-10-19, macOS arm64):**
- Tectonic 0.17.0 (`tectonic-0.17.0-aarch64-apple-darwin.tar.gz`, 21.7 MB, SHA-256
  `a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667`, matches the GitHub release digest).
- `tectonic --untrusted --keep-logs -o .build main.tex` (V1 interface; `.build` must exist) compiled a `xepersian`
  document with Vazirmatn v33.003 (OFL, loaded with `Path=fonts/`), mixed Persian/English, `\cite` + BibTeX, a
  table and an equation. Cold run 100 s (package download into `~/Library/Caches/TectonicProject.Tectonic`,
  43 MB); warm run 0.7 s; `-C` (only cached) recompiles offline in 0.8 s. BibTeX runs automatically; no extra
  bibliography tool needed. Only warning: no italic Vazirmatn shape (substituted).
- **Decision:** Tectonic is the default engine for both templates; system `latexmk -xelatex` is the alternative.
  The Persian template ships Vazirmatn Regular/Bold in the document's `fonts/` folder.
- **Sandbox finding:** `--untrusted` stops `\write18` and `\openout` outside the output folder, but **does not stop
  reads**: `\input{../outside.txt}` and `\input{/etc/hosts}` were read. Mitigation in Task 3: on macOS wrap the
  engine in `sandbox-exec` with a profile that denies `file-read*` under the home folder, `/private/etc`,
  `/private/tmp` and `/Volumes` except the document folder, the engine, its cache and font folders (tested: the
  `/etc/hosts` read is refused); on Linux use `bwrap` when installed; otherwise a pre-compile scan refuses
  `\input`/`\include`/`\openin`/`\includegraphics` with absolute or `..` paths, and the UI says the sandbox is
  partial.

### Task 1: Citation keys and BibTeX (W0)

**Files:** create src/litforge/writing/__init__.py, src/litforge/writing/bib.py, migration
src/litforge/storage/migrations/0026_citation_keys.sql; test tests/unit/test_writing_bib.py.

**Produces:**
- Table `citation_keys(paper_id PK REFERENCES papers ON DELETE CASCADE, key UNIQUE, user_set INTEGER, created_at)`.
- `citation_key(conn, paper_id) -> str`: `firstauthorYEARword` (ASCII-folded, lower case; first significant title
  word; `a`, `b`… on collision); created once and never changed unless the user sets it.
- `set_citation_key(conn, paper_id, key)` with validation (`[a-z0-9:_-]+`, unique).
- `bibtex_entry(conn, paper_id) -> str` and `write_bib(conn, paper_ids, path)`: `@article`/`@misc` from library
  metadata (authors, title with braces protecting capitals, year, venue, DOI, arXiv `eprint`, URL); preprints noted
  with `note = {Preprint}` when the cited copy is a preprint (version labels from paper versions).
- CLI `litforge export --format bibtex [--project]`.

**Tests:** key stability across calls and renames; collisions; non-ASCII authors (Persian names transliteration
fallback to the paper id prefix); escaping; a round trip through a BibTeX parser (bibtexparser in tests only, or a
small checker); deleting a paper removes its key.

### Task 2: Documents, templates and snapshots

**Files:** create src/litforge/writing/documents.py, src/litforge/writing/templates/{article,persian}/main.tex;
migration 0027_writing_documents.sql; test tests/unit/test_writing_documents.py.

**Produces:**
- Folder layout `<data>/projects/<project>/writing/<slug>/` with `main.tex`, `references.bib`, `figures/`,
  `.snapshots/`.
- Table `writing_documents(id, project_id, name, slug, engine, created_at, updated_at)`; the files are the source of
  truth.
- `create_document(conn, paths, project, name, template)`, `list_documents`, `read_file`, `save_file(text,
  expected_hash)` (atomic write; refuses when the file changed on disk since it was read, so two tabs or an external
  editor cannot silently overwrite each other), `upload_figure(name, bytes)` (images and PDF only, size cap),
  `delete_document` (moves to the project's trash folder).
- Snapshots: on every save at most one snapshot per 2 minutes, keep the last 30; `list_snapshots`,
  `restore_snapshot(id)` (the current text is snapshotted first).
- Path containment for every file name.

**Tests:** create from both templates; save conflict detection; atomic save (simulate failure between write and
rename); snapshot rotation and restore; traversal attempts (`../`, absolute paths, symlinks) rejected.

### Task 3: Compile engine (optional tool) and compile job

**Files:** create src/litforge/writing/compile.py; modify src/litforge/tools.py (Tectonic install/status),
src/litforge/ui/server.py (job), src/litforge/cli.py (`litforge write compile`); test
tests/unit/test_writing_compile.py.

**Produces:**
- Tectonic as an optional tool in Settings → Optional tools and `litforge tools install tectonic`: download the release
  binary for the platform from GitHub, verify SHA-256 against a pinned list, install under `<data>/tools/tectonic`;
  size shown before confirmation; status/version. System `xelatex`/`latexmk` detected as an alternative.
- `compile_document(doc, engine) -> CompileResult(pdf_path, log, errors[], warnings[], elapsed_ms)`:
  - Tectonic: `tectonic -X compile main.tex --untrusted --outdir .build` (untrusted disables shell escape and
    restricts file access); keep its package cache under `<data>/tools/tectonic-cache`.
  - XeLaTeX: `latexmk -xelatex -no-shell-escape -interaction=nonstopmode -halt-on-error -outdir=.build main.tex`
    with `openout_any=p` and `openin_any=p` in the environment (paranoid file access).
  - Runs through the existing tree-kill helpers with a time limit (default 120 s) and bounded output.
  - Log parser: `! ...` errors with `l.<n>` line numbers, file names, undefined references/citations as warnings.
- UI job `writing-compile` (one at a time per document) via `App.start_job`; the PDF is served by a read-only GET
  inside the document folder.

**Tests:** fake engine scripts (argv/env recorded) for both engines; shell-escape flags present; timeout kills a
grandchild; log parsing on real-world error samples (missing `}`, undefined control sequence, missing file, undefined
citation, Persian font missing); PDF served only from `.build/` of that document.

### Task 4: "Write" view in the UI

**Files:** create src/litforge/ui/static/vendor/codemirror.bundle.js (+ LICENSE files), scripts/build_editor_bundle.md
(how the bundle was built, pinned versions); modify src/litforge/ui/static/index.html, app.js, app.css,
src/litforge/ui/server.py, src/litforge/ui/api.py; tests tests/ui/writing.test.cjs, tests/integration/test_ui_writing.py.

**Produces:**
- Sidebar entry "Write" (per project): document list, "New document" (template picker: English article, Persian
  article), open, rename, delete (to trash).
- Editor: CodeMirror 6 with LaTeX highlighting (`@codemirror/legacy-modes` stex), search/replace, undo, line
  numbers, soft wrap, right-to-left text handled per line (CodeMirror bidi), Persian font in the editor.
- Auto-save after 2 s idle and on blur, with the conflict check from Task 2; "Saved" / "Conflict — reload or keep
  mine" state.
- Toolbar: Compile (job + status), Versions (snapshot list → restore), Upload figure, Cite (Task 5), AI actions
  (Task 6).
- Split view: editor left, PDF right in an `<iframe>` (browser PDF viewer), refreshed after a successful compile;
  error list under the editor, click jumps to the line.
- API: `GET /api/write/docs?project=`, `GET /api/write/file?doc=&name=`, `GET /api/write/pdf?doc=`,
  `GET /api/write/snapshots?doc=`; POSTs: create, save, upload, delete, restore, compile.
- The CodeMirror bundle is built once with esbuild from pinned npm packages and committed (no build step at run time;
  no CDN). Document how to rebuild it.

**Tests:** UI tests for list/create/open, auto-save debounce, conflict banner, compile status, error jump, RTL line
rendering smoke test; integration tests for every endpoint including origin guard and traversal attempts.

### Task 5: Citing from the library

**Files:** create src/litforge/writing/cite.py; modify api.py, server.py, app.js; tests
tests/unit/test_writing_cite.py, tests/ui/writing.test.cjs.

**Produces:**
- Cite palette (button and `\cite{` trigger): search the project library (title, authors, year, DOI) and, as a
  second tab, the evidence ledger (`search` with quotes and pages); version badge shown.
- Insert `\cite{key}` at the cursor (or add to the existing `\cite{...}` list); the paper's entry is added to the
  document's `references.bib` (Task 1), keeping user-edited entries untouched (only LitForge-managed entries between
  markers are rewritten).
- "Show quote" on a selected `\cite{key}`: the evidence the citation was inserted from (table
  `writing_citations(document_id, key, evidence_id, added_at)`), with a link to the PDF page.

**Tests:** insertion into empty and existing cite lists; bib sync preserves user entries; unknown keys left alone;
evidence link stored and shown.

### Task 6: Three AI actions on the selection

**Files:** create src/litforge/writing/assist.py and prompts in src/litforge/analysis/prompts.py (versioned
`write-v1`); modify gateway task map defaults (`write_fix`, `write_rewrite`, `write_cite` routed to the local profile),
api.py, server.py, app.js; tests tests/unit/test_writing_assist.py, tests/ui/writing.test.cjs.

**Produces:**
- `fix_error(doc, error)`: sends the error message and ±15 lines around it; returns a replacement for that range.
- `rewrite(selection, mode)`: modes clearer / shorter / translate (to English or Persian); returns new text.
- `suggest_citations(selection)`: retrieval from the project evidence (existing `retrieve_for_question`), the model
  picks which spans support which sentence; returns the text with `\cite{key}` inserted plus the quotes used.
- Every result is a diff the UI shows inline with Accept / Reject (per hunk) — nothing changes until accepted.
- Guards before showing a suggestion:
  - numbers in the suggestion that are not in the selection or the returned evidence → hunk flagged (reuse
    `missing_numbers`);
  - `\cite` keys that are unknown or not backed by returned evidence → flagged;
  - LaTeX commands, math and `\cite`/`\ref`/`\label` present in the selection must still be present (count check) →
    otherwise flagged;
  - for `suggest_citations`, each quote is verified with the exact-quote locator (`QuoteLocator`) before it is
    offered.
- Preview and approval: the request preview shows the model and what is sent; remote routes need approval as for
  Ask; budgets apply; each call is in `model_calls` with task and prompt version.
- Suggestions stored in `writing_suggestions` (id, document_id, task, model, prompt_version, range, diff, flags_json,
  status) for the record; accepted ones are noted in the snapshot that follows.

**Tests:** fake gateway replies for each action, including invented numbers, unknown keys, dropped `\cite` and
dropped math; Persian selection with inline LaTeX survives; cloud route without approval refused; quotes failing
verification are not offered; accept/reject per hunk in the UI.

### Task 7: Real-use check, docs and the review loop

**Files:** docs/guide.md (Write section), docs/cli.md regenerated, README unchanged except one line linking the
guide section if needed.

**Steps:**
- Real run on a copy of the user's data: create a Persian document from the template, cite three papers from the
  `vbi` project, compile with the engine chosen in Task 0, fix a deliberately broken line with `fix_error` using the
  local Gemma, rewrite a paragraph shorter, add citations to a paragraph; record times and any guard flags.
- Three review rounds before merge (Codex and/or a reviewer agent), each followed by fixes with regression tests.

**Acceptance (v1):**
- English and Persian templates compile out of the box with the chosen engine; recompile of a 10-page document
  in under ~5 s warm.
- Typing, saving, compiling and previewing work with the network off (after the engine and packages are installed).
- No text is lost in the save-conflict and crash tests.
- In the real run, every inserted `\cite` resolves and is backed by a verified quote; no unflagged invented number in
  the seeded guard tests.
- Compile cannot read or write outside the document folder in the sandbox tests.

## Order and size

Task 0 (S) → Task 1 (S) → Task 2 (S–M) → Task 3 (M) → Task 4 (M) → Task 5 (S–M) → Task 6 (M) → Task 7 (S + review
rounds). Tasks 1 and 2 can run in parallel; Task 4 needs 2 and 3; Task 6 needs 4 and 5.
