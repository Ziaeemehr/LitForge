# Writing Studio: a local LaTeX editor with evidence-aware language-model help

Status: plan, not started.

**Decision (2026-10-18):** the user keeps writing in VS Code. The path that will be built is **W0 + the LitForge
MCP server** described next: LitForge's evidence, citations and checks become tools for the AI assistants already
in VS Code (Claude Code, Copilot Chat, Continue), and LaTeX Workshop keeps doing editing, compiling and preview.
The built-in editor (W1–W4 below) stays in this document as a **future plan that is not scheduled**; it is only
reconsidered if the MCP path turns out not to be enough in practice.

## Chosen path: W0 + LitForge MCP server

### What the user does

In VS Code, with the `.tex` file open, the user asks the assistant things like "add citations from my library to
this paragraph", "is this sentence supported by the papers I cited?", "shorten this and keep the citations",
"write a related-work paragraph from my matrix on X". The assistant calls LitForge's tools, gets evidence with
exact quotes and pages, and proposes the edit in its own diff view, which the user accepts or rejects.

### Server

- `litforge mcp` starts an MCP server over stdio (the standard local transport); `--project` scopes it to one
  project, otherwise the active project. Read-only by default.
- Ready-made configuration snippets for Claude Code (`claude mcp add litforge -- litforge mcp`), VS Code
  (`.vscode/mcp.json` for Copilot Chat) and Continue; `litforge mcp --print-config <client>` prints them.
- Runs entirely locally; it talks to the same database and data folder as the app. It never calls a cloud model
  itself unless a tool explicitly uses the gateway and the user's model settings allow it.

### Tools (first version)

| Tool | What it returns / does |
|---|---|
| `search_evidence(query, k, papers?)` | Ranked evidence spans: paper, citation key, version label, page, section, text |
| `find_citations(text, k)` | For a sentence or paragraph: candidate papers with the supporting exact quote and page |
| `check_support(text, citation_keys?)` | Supported / partly / not found per sentence, with quotes (reuses Ask retrieval and the quote validator; local model by default, deterministic matching where possible) |
| `check_text(text, original?)` | Guards for an edit the assistant wrote: numbers and `\cite` keys not present in the original or the returned evidence are flagged; unknown keys; preprint where a published version exists |
| `get_paper(key or id)` | Metadata, abstract, versions, PDF status |
| `matrix(name)` / `claims(topic)` | Study-matrix rows and claim clusters with their evidence, for drafting related work |
| `bibtex(keys or project)` | BibTeX entries with stable keys (W0) |
| `add_to_bib(path, keys)` | **Write tool**, off unless `--allow-write`: appends entries to the user's `.bib` file (only inside the workspace folder) |

Every tool result carries the evidence ids, so the assistant can cite precisely and `check_text` can verify.

### Privacy

- Tool results contain library text, and the assistant's model (often a cloud model) will see it. The first run
  shows this clearly; `litforge mcp` refuses to start for a project marked sensitive unless `--allow-sensitive`.
- Results are evidence snippets, never whole PDFs; a size cap applies per call.
- Each call is logged (tool, project, number of spans returned) in the same audit area as model calls.

### Optional later: VS Code extension

Only if wanted after using the MCP path: `\cite{` completion from the library, hover showing the quote behind a
citation, and squiggles from `check_text`/citation check, without going through the chat.

### Acceptance

- In Claude Code and Copilot Chat on a real `.tex` file: "add citations to this paragraph" yields only keys that
  exist, each backed by a returned quote; `check_support` agrees with a human check on a 30-sentence sample in
  ≥ 90 % of cases; `check_text` flags every invented number in a seeded test set.
- No write happens without `--allow-write`; no file outside the workspace is touched.

---

# Future plan (not scheduled): built-in editor

Phases are independent deliverables; each would get its own implementation plan and the usual review rounds.

## Why

LitForge already finds, reads, compares and cites papers, but writing happens elsewhere (Overleaf or a local
editor), so the link between a sentence in the manuscript and the evidence behind it is lost. The goal is an
Overleaf-like editor inside LitForge where:

- the user writes LaTeX with a live PDF preview, offline;
- citations come from the library and stay connected to verified evidence (exact quotes, pages);
- language models suggest edits (clarity, length, grammar, translation, LaTeX fixes, drafting from evidence) as
  reviewable diffs, never silently, and never introduce facts the evidence does not support.

## Principles (same as the rest of LitForge)

- Local first: files live in the data folder as plain `.tex`/`.bib`; compiling and editing work offline.
- Nothing leaves the machine without explicit permission; a cloud model sees only the selected text and the
  evidence the user chose, through the existing model gateway (routing, approval, budget, audit log).
- Every model suggestion is a diff the user accepts or rejects; the editor never applies edits on its own.
- A suggestion that adds a number, claim or citation not found in the source text or the cited evidence is
  flagged, reusing the quote/number validation already used by Ask.
- Projects stay portable: a writing project exports as an Overleaf-compatible ZIP and imports from one.

## Phases

### W0. Citation keys, BibTeX and LaTeX export (foundation, small)

- Stable citation keys per paper (`ziaeemehr2025virtual`: first author + year + first significant title word,
  with `a/b` suffixes on collision); stored once, never changed after first use; user-editable.
- `references.bib` for a project or selection (BibTeX; fields from the library metadata: authors, title, year,
  venue, DOI, arXiv id, URL; the version of the cited copy noted when it is a preprint).
- Briefs, Ask answers and the study matrix export as `.tex` with `\cite{key}`; every exported citation comes from a
  verified quote (sentences whose quotes failed validation are exported as comments for review).
- CLI `litforge export --format latex|bibtex`; UI export menu.

### W1. Writing projects, editor and compile

- A project can hold writing documents: `<data>/projects/<name>/writing/<doc>/` with `main.tex`, other files,
  `references.bib` (kept in sync with W0) and figures. Each document folder is a small git repository for history
  (auto-commit on save points, named snapshots, restore, diff view).
- Editor: CodeMirror 6 with LaTeX highlighting, folding, search/replace, outline from `\section`, spell check;
  vendored as a pre-built bundle under `ui/static/vendor/` (no CDN, works offline; licences recorded).
- Compile: Tectonic (self-contained TeX engine) offered as an optional tool in Settings → Optional tools
  (download size shown, checksum-verified). First compiles fetch TeX packages from Tectonic's bundle server once
  (disclosed; afterwards cached and offline). An existing TeX Live/MacTeX `latexmk` is used if the user prefers.
- Safety: shell escape off, reads/writes limited to the document folder, time limit, process tree killed on
  timeout or shutdown (reuse the MinerU/job helpers).
- Preview: PDF.js (vendored), auto-recompile on pause, SyncTeX jump editor ↔ PDF, compile log with clickable
  errors and warnings.
- Import an Overleaf/LaTeX ZIP; export the document as ZIP (Overleaf "Upload project" compatible).

### W2. Evidence-aware citing

- Cite palette (`\cite` autocomplete and a side panel): search the library and the evidence ledger; each result
  shows the paper, version badge, page and the exact quote; inserting adds the key to `references.bib`.
- Hovering a `\cite{key}` shows the paper and the quotes the citation was taken from; click opens the PDF page.
- Citation check (on demand and before export): unknown keys, papers outside the project, preprint versions when a
  published version exists, retracted papers, and sentences that state results but cite nothing (heuristic,
  shown as hints, never blocking).
- "Support check" for a selected sentence: retrieves evidence from the cited papers and reports supported /
  partly supported / not found, with quotes (reuses Ask retrieval and audit; local model by default).

### W3. Language-model assistance

Selection-scoped actions, each a separate gateway task so the user can route it to a different model in
Settings (e.g. a small local model for grammar, a stronger one for drafting):

| Action | Input sent to the model | Output |
|---|---|---|
| Improve clarity / shorten / formal tone | selected text | diff |
| Grammar and spelling | selected text | diff |
| Translate (e.g. Persian ↔ English) | selected text | diff or new block |
| Fix LaTeX error | compile error + surrounding lines | diff |
| Suggest citations | selected sentence | ranked evidence from the library, insert on click |
| Draft from evidence | a short instruction + evidence the user picked in the cite panel | new paragraph with `\cite` |
| Explain / summarise a paragraph | selected text | side note, not inserted |

- Suggestions appear as inline diffs with accept/reject per hunk, plus "accept all" and undo; history records
  which edits came from which model (model, task, prompt version, time) in the document's git log.
- Guards: numbers and quoted claims in a suggestion must appear in the selection or the provided evidence; new
  `\cite` keys must exist and must point to evidence that was in the request; otherwise the hunk is flagged.
- Privacy: the request preview shows exactly what will be sent and to which model; cloud models need approval as
  elsewhere; budgets apply; nothing is sent automatically while typing (no background completions in v1).
- Prompts are versioned like the other prompts; an evaluation set (paragraph + expected properties) measures
  meaning preservation, invented facts and citation validity per model.

#### Parity with Overleaf AI (checked 2026-10-18)

Overleaf (with Writefull) offers these in its editor; the Writing Studio covers each, locally where possible and
grounded in the library where Overleaf is not:

| Overleaf feature | Writing Studio equivalent | Difference |
|---|---|---|
| AI assistant (chat with the document, LaTeX help, runs tools) | Side-panel assistant over the open document **and** the project's evidence; answers cite library quotes | Can answer "what does the literature say" from verified evidence; local model by default |
| Error Assist (explain compile errors, suggest fix) | "Fix LaTeX error" action on the compile log | Sends only error + nearby lines, to a local model by default |
| Language suggestions (grammar/fluency, inline) | Grammar action plus optional inline hints from a small local model | No text leaves the machine unless a cloud model is chosen |
| Rewrite tools: paraphrase, make concise, make scientific, split/join sentences | Same actions on the selection, as diffs | Numbers/citations guarded |
| Generate title and abstract | Title/abstract from the document's sections | Abstract claims checked against the document's own text |
| TeXGPT (natural language → LaTeX) | "Write LaTeX for…" action (figures, tables, environments, macros) | — |
| Equation generator (from text or an image) | Equation from text, or from an image/PDF crop using the existing vision and formula readers (MinerU, Pix2Text) | Can take the equation straight from a cited paper's page |
| Table generator (from a prompt or an image) | Table from text, from an image, or from a library paper's parsed table / the study matrix | Values come from the parsed table, not re-typed |
| Synonyms in context | Synonym suggestions for the selected word | Local |
| Citation check / citation suggestions (via Dimensions) | W2 citation check and cite suggestions from the user's own library and evidence ledger | Suggestions come with the exact supporting quote |
| Sentence completion (Supercomplete) | Optional, off by default, local model only, never sends text while typing to a cloud model | Opt-in per document |
| Summarize / explain (removed from Overleaf, still in Writefull) | Explain/summarise selection as a side note | Not inserted into the manuscript |

Overleaf also lets users and institutions turn all AI off; the Writing Studio follows the existing model
settings (local only / ask before cloud / budgets) and adds a per-document "no AI" switch.

### W4. Sync and collaboration (later, optional)

- Git remote sync for a writing document (works with Overleaf's git integration on paid plans, or any git host).
- Comments and review mode for a co-author on the same machine/network. Real-time multi-user editing is out of
  scope.

## Ideas taken from Overleaf (non-AI)

Ranked by value for a single researcher working locally; "later" items are not part of the first version.

| Overleaf feature | In the Writing Studio | When |
|---|---|---|
| Templates gallery (journals, theses, Persian theses) | A few bundled templates: article, thesis, Persian article/thesis (`xepersian`), a journal template the user adds by ZIP | W1 |
| Reference manager sync (Zotero, Mendeley) | Not needed for the library itself; `references.bib` is generated from LitForge (W0). Import an existing `.bib` keeps the user's keys | W0 |
| Autocomplete for `\cite`, `\ref`, `\label` and commands | CodeMirror completion from `references.bib`, labels in the document and a LaTeX command list | W1 (`\cite` W2) |
| Outline / document structure panel | From `\section` etc.; click to jump | W1 |
| Choose compiler and main file (pdfLaTeX, XeLaTeX, LuaLaTeX) | Per document: engine (Tectonic or system XeLaTeX/LuaLaTeX via `latexmk`) and main file | W1 |
| Inline compile errors, "stop on first error", draft mode | Error list with jumps; draft/fast compile option | W1 |
| PDF ↔ source sync (SyncTeX) | Double-click in PDF ↔ jump in source | W1 (may slip to W1.1) |
| History with labelled versions, compare, restore | Git per document: auto-saves, named versions, diff, restore | W1 |
| Track changes and comments | Model suggestions already arrive as accept/reject diffs (W3); comments on lines for the user's own notes | W3 / later |
| Word count | Word count of the compiled text (excluding commands), also for Persian | W1 |
| Symbol palette | Searchable math-symbol palette | later |
| Visual (rich-text) editor | Not planned; a math/figure preview on hover instead | later |
| Spell check with dictionaries | Browser spell check; English and Persian dictionaries | W1 |
| Git / GitHub / Dropbox sync | Git remote sync (W4); Dropbox not planned | W4 |
| Real-time collaboration, sharing links, chat | Out of scope (local-first, single user) | — |
| Submit to journal | Out of scope | — |

## Scope and feasibility

The full plan is large, mostly because of W1 (a real editor with compile, preview and sync). It is feasible with
the existing pieces (job runner, optional-tool installer, model gateway, evidence ledger), but it should be built
as a sequence of small, usable releases, not all at once.

Rough sizes (S ≈ a few days, M ≈ one to two weeks, L ≈ several weeks, each including review rounds):

| Part | Size | Main risk |
|---|---|---|
| W0 citation keys, `.bib`, `.tex` export | S | none significant |
| Spike: Tectonic + `xepersian` + Persian fonts | S | Tectonic may not support it; then system XeLaTeX |
| W1-lite: editor, compile, PDF preview, error list, snapshots, templates | M | bundling CodeMirror and PDF.js without a build step |
| W1 full: SyncTeX, autocomplete, outline, word count, ZIP import/export | M | SyncTeX with RTL text |
| W2 evidence-aware citing | M | — |
| W3 AI actions (start with 3: fix error, rewrite selection, suggest citations) | M | evaluation set and invented-fact guards |
| W3 remaining actions (title/abstract, equation/table from image, assistant panel) | M–L | — |
| W4 git remote sync | S–M | conflicts with Overleaf edits |

**First usable version (recommended):** W0 → spike → W1-lite → the three most useful AI actions. That already gives
"write in LitForge, cite from the library, fix errors and polish text with a local model", with less than half the
total work. Everything else follows only if this version is used in practice.

**Cheaper alternative, if the editor itself is the bottleneck:** keep writing in an existing editor (VS Code with
LaTeX Workshop, TeXstudio or Overleaf) and make LitForge a companion: W0 export, a "cite" and "check citations"
panel in the LitForge UI that edits the `.tex` file on disk, and the AI actions applied to a selected range of that
file. This skips W1 almost entirely; the cost is a less integrated experience (two windows, no shared preview).

## Data model (sketch)

- `citation_keys(paper_id PK, key UNIQUE, created_at, user_set)`.
- `writing_documents(id, project_id, name, path, engine, created_at, updated_at)`; files on disk are the source of
  truth.
- `writing_suggestions(id, document_id, task, model, prompt_version, input_hash, selection_range, diff, flags_json,
  status: open|accepted|rejected, created_at)`; links to `model_calls` for audit and cost.
- Evidence links: `writing_citations(document_id, key, evidence_id, added_at)` written when a citation is inserted
  from the palette, so the support check and hover know the quotes behind it.

## Not in scope (for now)

WYSIWYG editing, real-time collaboration, managing a full TeX Live installation, automatic background completions,
journal submission integrations.

## Acceptance criteria

- Offline after setup: edit, compile, preview and history work with the network disabled (once Tectonic's package
  cache is filled for the document's packages).
- A 30-page document recompiles in under ~5 s on the reference Mac; SyncTeX jumps land on the right line.
- In the evaluation set, accepted W3 suggestions introduce zero unflagged numbers or citations; meaning preserved
  in ≥ 95 % (human-rated sample).
- Every `\cite` inserted from the palette resolves to a key in `references.bib` and to stored evidence.
- Overleaf round trip: export ZIP → upload to Overleaf → compiles; Overleaf download ZIP → import → compiles.

## Risks and open questions

- Bundling CodeMirror and PDF.js without a build step: ship pre-built, pinned bundles with licences; decide on
  updating them.
- Tectonic covers most LaTeX documents; some need `biber`/`biblatex` or packages it cannot fetch — fall back to a
  system `latexmk`, and say which engine compiled the PDF.
- Long documents and model context limits: actions stay selection-scoped; drafting uses an evidence packet, not
  the whole manuscript.
- Persian is a requirement, not an option: documents use XeLaTeX with `xepersian` (bidi) and Persian fonts
  (e.g. XB Niloofar / Vazirmatn, bundled or downloaded with consent). W1 must ship a tested Persian template and a
  mixed Persian/English template, compile both with the chosen engine, and check that SyncTeX, search and the
  editor handle right-to-left text. If Tectonic cannot compile `xepersian` reliably, the Persian templates use the
  system XeLaTeX (TeX Live/MacTeX) and Settings says so; this is decided by a spike before W1 starts.
- Language-model help in Persian: grammar/rewrite/translation actions must work on Persian text (prompt and
  evaluation set include Persian paragraphs); LaTeX commands inside Persian text must survive edits unchanged.
- Where the writing studio lives in the UI (a new top-level view per project) and how it relates to briefs
  (a brief can be "opened in Writing Studio").
