# Writing Studio: a local LaTeX editor with evidence-aware language-model help

Status: plan, not started. Phases are independent deliverables; each gets its own implementation plan and the
usual review rounds before merge.

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

### W4. Sync and collaboration (later, optional)

- Git remote sync for a writing document (works with Overleaf's git integration on paid plans, or any git host).
- Comments and review mode for a co-author on the same machine/network. Real-time multi-user editing is out of
  scope.

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
- Persian/RTL text in LaTeX needs XeLaTeX + `xepersian` and suitable fonts: verify Tectonic supports the templates
  the user needs, and ship one tested Persian template.
- Where the writing studio lives in the UI (a new top-level view per project) and how it relates to briefs
  (a brief can be "opened in Writing Studio").
