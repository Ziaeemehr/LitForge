# Living Review, Reproducibility, Figure Intelligence, Gap Designer, Research Console (roadmap phases 3–5) — design

**Date:** 2026-10-17
**Builds on:** phases 0–2 (gateway, ledger, runs, ask/audit, matrix, human reviews, claims, full-text screening).
Each phase is implemented and reviewed separately, in this order.

---

## Phase 3 — Living Review + Reproducibility Radar

### 3.1 Snapshots (`litforge.analysis.snapshots`)

- `take_snapshot(conn, project_id, label="") -> str` stores an immutable `project_snapshots(id, project_id,
  label, created_at, content_hash, data_json)` where `data_json` holds, per project:
  `papers` (id, title, year, doi, screening decisions S1/S2, mark), `matrix` (per matrix: cells with status,
  value, evidence IDs, cache key), `claims` (id, canonical hash, cluster id, status, evidence IDs),
  `relations` (pair, relation), `answers` (latest ask run per question: sentence texts, kinds, evidence IDs).
  Update/delete triggers keep snapshots immutable.
- `diff_snapshots(old, new) -> SnapshotDelta`: added/removed papers; screening decision changes; matrix cells
  changed (value/status/evidence) per column; claims added/removed; clusters whose support or contradiction
  count changed (**strengthened** / **weakened**); claims still `single_source`; answer sentences whose
  evidence set changed (**answer may need rewriting**).
- `significance(delta) -> list[Signal]` with explicit rules: new included paper with priority ≥ the 75th
  percentile; cluster strength change ≥ 1; new contradiction; any changed reported matrix value; answer
  evidence change. Counting new papers alone is never significant.

### 3.2 Refresh (`litforge.analysis.living`)

- `refresh_project(conn, gateway, sources, project_id, *, screen=True, extract=True, claims=True,
  progress=None) -> WatchRun`: take a "before" snapshot (or reuse the latest); re-run the project's stored
  discovery queries (`discovery_runs` of the project, deduped by query) with the existing discovery code, which
  already dedupes; screen only papers new since the previous snapshot with the same criteria (S1, and S2 if a
  parse exists); extract matrix cells only for new/changed papers (cache keys make this incremental); extract
  claims for new papers, re-cluster, relate only pairs involving new claims; take the "after" snapshot; store
  `watch_runs(id, project_id, before_snapshot, after_snapshot, delta_json, signals_json, status, started_at,
  finished_at)`.
- Delta report Markdown: important papers added, results strengthened/weakened, changed matrix cells, whether
  the conclusion needs rewriting, claims still without replication. Written to the project exports.
- Optional schedule: `litforge watch install --every 7d` writes a launchd plist (macOS) / systemd user timer
  (Linux) / prints a Task Scheduler command (Windows) that runs `litforge refresh -p NAME`; `watch remove`.
  Nothing is installed without the command. Notifications: the refresh command exits 0 and prints signals;
  `--notify` uses the OS notifier (osascript / notify-send) only when significant signals exist.
- CLI: `litforge snapshot [-p] [--label]`, `litforge snapshots [-p]`, `litforge delta OLD NEW [--md FILE]`,
  `litforge refresh [-p] [--no-screen] [--no-extract] [--no-claims] [--notify]`, `litforge watch install|remove`.

### 3.3 Reproducibility Radar (`litforge.analysis.repro`)

- A built-in matrix template `reproducibility` (columns: data availability, dataset names, code availability,
  repository URLs, software and versions, random seeds, preprocessing, hyperparameters, hardware, data split,
  metrics, compute budget) extracted with the phase-1 matrix machinery.
- Deterministic link extraction from the paper's full text (all chunks): URLs matching GitHub, GitLab,
  Bitbucket, Zenodo, Figshare, OSF, Hugging Face, Kaggle, Dryad, PhysioNet, OpenNeuro, DOI-based data DOIs;
  stored as `repro_links(paper_id, url, kind — code|data|other, evidence_id)`. Links are never fetched or run.
- `repro_card(conn, paper_id)`: checklist items present / missing / unclear, links, and a
  `reproduction brief` (Markdown): what data is needed, which steps are unclear, which parameters to ask the
  authors, and a rough compute estimate **only** when hardware/time is reported (otherwise "not reported").
  Brief sentences follow the phase-1 rule: cited or labelled.
- CLI `litforge repro [-p] [--paper ID] [--md FILE]`; UI: **Repro** tab in the paper drawer.

---

## Phase 4 — Figure/Table/Equation Intelligence + Gap Designer

### 4.1 On-demand asset analysis (`litforge.analysis.figures`)

- Only assets selected by retrieval for a question or a matrix column (or picked by the user) are processed;
  never all pages. Asset rows already exist (`assets` table with crop paths and bbox).
- `analyze_asset(conn, gateway_vision, asset_id, mode)` with modes:
  `table` → `{"columns": [str], "rows": [[str]], "units": {col: unit}, "footnotes": [str]}`;
  `chart` → `{"axes": {...}, "legend": [str], "series": [{"name", "points": [[x, y]]}], "error_bars": str|null}`
  where every point is labelled `digitized_estimate`;
  `equation` → `{"latex": str, "symbols": [{"symbol", "meaning", "evidence": "caption|text|unknown"}]}`.
- Vision calls use the local vision model through the gateway (a `vision` capability on providers: Ollama
  `images`, OpenAI-compatible `image_url` content) with the crop only; cloud vision follows the same policy and
  approval rules. Results stored as `asset_analyses(id, asset_id, mode, result_json, model, prompt_version,
  fidelity_json, run_id, created_at)` and as evidence spans of kind `table_cell` / `figure_estimate` /
  `equation` (text = a rendered summary) so they can be cited — but with `kind` preserved so values read from
  images are never treated as numbers reported in text (matrix validation flags `from_image`).
- Fidelity checks, recorded separately: table cell numbers present in the page's parsed text (fraction);
  units recognised; legend entries found in the caption; equation symbols found in surrounding text.
- Symbol glossary across papers: `glossary(conn, project_id)` groups symbols by normalised meaning; formulation
  comparison `compare_equations(a, b)` via one model call returning shared/different assumptions, each tied to
  evidence or labelled unknown.
- CLI `litforge asset analyze ASSET_ID --mode table|chart|equation`, `litforge glossary [-p]`; the UI shows
  results next to the crop.

### 4.2 Gap & Experiment Designer (`litforge.analysis.gaps`)

- Candidate gaps are computed **only** from the claim landscape and matrices, with deterministic detectors:
  single-source claims; contradicted clusters; matrix columns where a category or population appears in < 2
  papers; missing baselines (a categorical column value common elsewhere absent for a cluster); parameters
  without sensitivity analysis (matrix column flag). Each candidate carries its supporting evidence IDs.
- `design_gap(conn, gateway, gap_id)` (task `gap_design`, prompt `gap-v1`): card with why it matters, minimal
  experiment/analysis, data, controls, outcome, failure modes, and **what the model does not know**; every
  sentence cited or labelled `proposal`.
- Tables `gap_candidates`, `gap_cards` with `review_status` (`open` by default). Export of a gap card as a
  result or formal proposal requires `review_status = accepted` (enforced in code). Cards are titled
  "Research proposal — not a literature finding".
- CLI `litforge gaps [-p]`, `litforge gaps design ID`, `litforge gaps export ID`; UI **Gaps** view.

---

## Phase 5 — Limited automation + Research Console

### 5.1 Action registry (`litforge.console.actions`)

- A closed registry of typed actions; each has a pydantic args model, a description, `cost_kind`
  (`none`|`local_model`|`model`|`network`), `writes` (bool) and an executor. Initial actions:
  `filter_papers` (year range, has_parse, has_code (repro link), text match in title/abstract, decision,
  mark), `search_library`, `ask`, `create_matrix`, `add_column`, `extract_matrix`, `extract_claims`,
  `cluster_claims`, `relate_claims`, `screen_fulltext`, `export_matrix`, `write_brief`, `snapshot`, `refresh`.
- No action executes SQL, shell, file paths outside the project export folder, or network calls other than
  the existing discovery sources and configured model providers.

### 5.2 Planner (`litforge.console.planner`)

- `plan(conn, gateway, request_text, project_id) -> Plan`: one model call (task `console_plan`) with the
  registry's JSON schema (the action names as an enum, args per action); the request text is user input, the
  library is not sent. The plan is validated against the registry; unknown actions or invalid args are
  rejected with the validation errors shown, never "fixed" silently. Steps may reference outputs of earlier
  steps by `$step_n` (paper sets, matrix ids).
- `preview(plan)`: per step — action, args, number of papers in scope (dry evaluation of filters), model
  calls estimate and max cost via the gateway's preview, time estimate (calls × configured seconds per call),
  writes or not.
- `execute(plan, approved=True)`: runs steps in order, each a checkpoint row in `console_runs(id, project_id,
  request, plan_json, status, current_step, results_json, created_at, updated_at)`; resumable after a failure;
  cancel between steps. Every model call stays inside the gateway's policy/budget checks.

### 5.3 Workflow templates

YAML templates in `configs/workflows/` — `systematic_review.yaml`, `scoping_review.yaml`, `deep_dive.yaml` —
each a parametrised plan (e.g. question, years) that loads into the same preview/execute path.

### 5.4 CLI and UI

- `litforge do "REQUEST" [-p] [--yes]` → shows preview, asks to confirm (or `--yes`), executes;
  `litforge workflow run NAME --param k=v`; `litforge console runs` / `resume ID`.
- UI **Console**: request box → plan preview table → Run; progress per step; links to the produced matrix,
  answer, brief.

## Testing (all phases)

Fakes only (FakeLlm, fake discovery sources, fake vision). Every phase: unit tests for each deterministic rule
named above; integration flow on the synthetic PDFs; UI endpoint tests where a view is added; immutability
triggers; approval/budget paths. Full suite, Ruff and strict mypy green after each phase.
