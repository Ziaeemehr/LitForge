# Review Reporting and Evidence Features (ideas from Bango) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This document is a proposal for review, not authorization to start implementation.

**Goal:** Add ten features, taken as ideas from Bango, that make LitForge stronger at systematic-review reporting and at checking a manuscript against the evidence. They must not change LitForge's evidence-first rules.

**Architecture:** Each feature is a small Python module behind the existing patterns: a pure function over the SQLite library, a `litforge` CLI command, a JSON route and a panel in `app.js`. Model calls go through `ModelGateway` with a task name in `TASK_NAMES`. A model's output counts only through deterministic checks, such as a verified quote, a syntax check or a count. One shared module resolves each paper's final screening status. PRISMA, the reference export and the bibliometrics all use it, so their counts always agree.

**Tech Stack:** Python 3.12, stdlib `sqlite3` with numbered migrations, pydantic, typer, the stdlib HTTP server, vanilla JavaScript, pytest and node:test. No new runtime dependency.

**Spec:** [docs/bango_comparison_fa.md](../../bango_comparison_fa.md), the comparison report (section 2 lists the ideas in priority order; section 4 lists what is out of scope). Related plan: [2026-10-23-desktop-app.md](2026-10-23-desktop-app.md).

## Embedded design

### Scope and order

- **Milestone A** (package 1, low cost): Tasks 1–5. These are PRISMA with the shared status module, two-stage screening, the search-string builder, RIS/BibTeX export with decisions, and Obsidian links.
- **Milestone B** (package 2): Tasks 6–10. These are criteria priorities and rules, the paragraph claim check, light bibliometrics, two-way Zotero, and translation of non-English papers.
- **Ship rule:** each task is shippable on its own. Milestone B tasks depend only on Task 1's status module.
- **Out of scope:** an LLM-generated wiki, the full set of six bibliometric modules, Google Trends, and desktop packaging. The desktop plan owns packaging.

### Final screening status (shared rule, Task 1)

A paper's final status in a project resolves in this order:

1. **Human mark:** `paper_marks.mark` = `relevant` gives `included`. A mark of `irrelevant` gives `excluded` at the latest stage the paper reached.
2. **Full-text result:** the S2 decision in `fulltext_screening`.
3. **Abstract result:** the S1 decision in `screening_results`.
4. **Neither:** the paper is `unscreened`.

`uncertain` stays `uncertain`; it is never counted as included. Only the primary version of a paper counts (`primary_paper_ids`). Other versions count as duplicates.

### Coordination with the desktop plan

These features run inside the desktop window planned in `2026-10-23-desktop-app.md`. The UI must therefore not depend on behaviours that differ between a browser and a webview:

- **Files on the server:** files (SVG, RIS, BibTeX, Obsidian notes) are written by the server into the project's export folder (`default_export_dir`). The UI shows the path and an "open folder" action. Downloads through `<a download>` or `window.open` are not used.
- **External links:** links such as a Zotero item or a database search page go through the same external-link routing that desktop Task 6 defines. In browser mode they are a plain `target="_blank"` link.
- **Route module:** new routes live in a new module, `src/litforge/ui/review_routes.py`, which follows the `writing_routes.py` pattern. This keeps `server.py` merge conflicts with desktop Tasks 2 and 5 small.

## Global Constraints

- **Migrations:** schema changes are new numbered migrations starting at `0033_*.sql`. Never edit an earlier migration. Existing libraries must upgrade in place.
- **Evidence:** no model output changes a decision without a verified quote. A verified quote is an exact match after whitespace normalisation inside a supplied evidence span, as in `screening/fulltext.py`.
- **Human marks:** human marks always win over model decisions and are never overwritten by a model run.
- **Model tasks:** every new model task is added to `TASK_NAMES` in `src/litforge/config.py`, defaults to the local route, and supports `--preview` and approval through `ModelGateway.preview` and `ModelGateway.call`. The user's manuscript text is sent with `sensitive=True`.
- **Criteria hash:** a project that sets no priorities or rules must keep its current criteria hash and prompt fingerprints. Upgrading must not trigger a rescreen.
- **CLI:** every feature has a CLI command with `--json` output, documented in `docs/cli.md` and `docs/guide.md`. `tests/unit/test_cli_docs.py` must keep passing.
- **Persian text:** Persian text in the UI renders right to left (the existing `dir="auto"` handling).
- **Network:** no new network service except Zotero on `127.0.0.1:23119`, and only when the user starts a Zotero action.

## Review Focus

- **Upgrade without rescreening:** an old project upgraded to this version must keep its screening cache. Task 6 adds a test that `criteria_hash` is unchanged for criteria without priorities.
- **Several versions of one paper:** PRISMA and the exports must count one record, not two. Task 1 adds a test with two linked versions.
- **Human mark against a model:** an `irrelevant` mark after an S2 `include` is counted as excluded at S2, with the reason "excluded by reviewer". Task 1 adds a test.
- **Imported files with odd metadata:** a paper with no year, no DOI, no authors or a Persian title must still export valid RIS and BibTeX and read back through `parse_ris` and `parse_bib`. Task 4 adds a round-trip test.
- **Zotero closed or local API disabled:** each case gets a clear message saying what to enable, not a stack trace or a hang. Task 9 adds tests for a refused connection and for a 403 reply.

## File and responsibility map

| Files | Responsibility |
| --- | --- |
| `src/litforge/screening/status.py` | Final status per paper (Task 1); used by PRISMA, exports and bibliometrics |
| `src/litforge/screening/prisma.py` | PRISMA 2020 counts and SVG |
| `src/litforge/screening/fulltext.py` | `only="borderline"` selection (Task 2) |
| `src/litforge/screening/criteria.py`, `decide.py`, `prompt.py` | Priorities and rules (Task 6) |
| `src/litforge/analysis/search_strategy.py` | Boolean strings per database, with syntax checks |
| `src/litforge/export/references.py` | RIS/BibTeX with decisions |
| `src/litforge/export/markdown.py` | Obsidian links and decision tags |
| `src/litforge/writing/claim_check.py` | Paragraph claim check against the evidence |
| `src/litforge/analysis/bibliometrics.py` | Timeline and bibliographic-coupling clusters |
| `src/litforge/discovery/zotero.py` | Zotero local API read and connector write |
| `src/litforge/analysis/paper_translate.py` | Title and abstract translation kept next to the original |
| `src/litforge/ui/review_routes.py` | JSON routes for all of the above |
| `src/litforge/ui/static/app.js`, `app.css` | Panels: Review report, Strategy, Check claims, Bibliometrics, Zotero |
| `src/litforge/cli.py` | Commands: `prisma`, `strategy`, `export-refs`, `biblio`, `zotero`, `translate-papers`; flags `screen --fulltext --borderline` and `export --obsidian` |
| `tests/unit/test_*.py`, `tests/integration/test_review_reporting.py`, `tests/ui/review.test.cjs` | Tests named in each task |

## Milestone A: Review reporting (package 1)

### Task 1: Final status and the PRISMA 2020 diagram

**Files:** Create `src/litforge/screening/status.py`, `src/litforge/screening/prisma.py`, `src/litforge/ui/review_routes.py`, `tests/unit/test_screening_status.py`, `tests/unit/test_prisma.py`, `tests/ui/review.test.cjs`. Modify `src/litforge/cli.py`, `src/litforge/ui/server.py` (dispatch to `review_routes`), `app.js`, `docs/guide.md`, `docs/cli.md`.

**Interfaces:**

- **`final_status`:** `final_status(conn, project_id: str) -> dict[str, PaperStatus]`, where `PaperStatus(BaseModel)` has the fields `status: Literal["included","excluded","uncertain","unscreened"]`, `stage: Literal["S1","S2","none"]`, `source: Literal["human","model","none"]` and `reason: str`. The `reason` for an S2 model exclusion is the text of the first violated exclusion criterion whose quote is verified.
- **`prisma_counts`:** `prisma_counts(conn, project_id: str) -> Prisma`, where `Prisma` holds the following counts as `int`. Identification follows PRISMA 2020's two columns:
  - `identified_by_source: dict[str,int]` counts databases and imports: search runs and imported files, keyed by `discovery_hits.source`.
  - `identified_other: dict[str,int]` counts other methods: hits with `relation` in `references`/`cited_by`, similar-paper and link runs, plus papers with `project_papers.source = 'manual'` and no hit.
  - A record is a distinct `(source, paper_id)` pair, so running the same search twice does not inflate the count. Only papers still in the project count.
  - `duplicates_removed` is the sum of records minus the unique primary papers.

  The remaining fields are `awaiting_fulltext`, `screened`, `excluded_s1`, `awaiting_screening`, `sought`, `not_retrieved`, `assessed`, `excluded_s2_by_reason: dict[str,int]`, `included` and `uncertain`.
- **`prisma_svg`:** `prisma_svg(p: Prisma, title: str) -> str` returns standalone SVG with fixed fonts and no scripts.
- **CLI:** `litforge prisma -p NAME [--json] [-o FILE.svg]`. Without `-o`, the SVG is written to `<export folder>/prisma.svg`.
- **Routes:** `GET /api/review/prisma?project=` returns the counts plus the SVG text. `POST /api/review/prisma/save` writes the file and returns `{path}`.

Steps:

- [ ] **Write the status tests.** Write `test_screening_status.py` with these cases, each asserting the exact `PaperStatus`:
  - a human `relevant` mark wins over an S1 `exclude`;
  - an `irrelevant` mark after an S2 `include` gives `excluded/S2/human`;
  - S2 wins over S1;
  - a paper with no results is `unscreened`;
  - only the primary version counts.
- [ ] **Write the PRISMA tests.** Write `test_prisma.py` on a seeded library: one search run with 5 hits from `openalex` and 3 from `crossref`, where 2 are the same paper; one import of 2 records.
  - Assert `identified_by_source == {"openalex":5,"crossref":3,"import":2}`, `duplicates_removed == 2`, and that `screened + awaiting_screening` equals the number of unique primary papers.
  - Seed more cases: the same search run twice (counts unchanged), one citation hit and one manual paper (both in `identified_other`), and one paper removed from the project (not counted). Assert the invariant `sum(identified_by_source) + sum(identified_other) - duplicates_removed == unique primary papers in the project`.
  - Assert `sought - not_retrieved == assessed + awaiting_fulltext`, where `awaiting_fulltext` covers papers that have a PDF but no S2 result yet.
  - Assert the reasons in `excluded_s2_by_reason` sum to the S2 exclusions.
  - Assert the SVG parses with `xml.etree.ElementTree` and contains every count as text.
- [ ] **Confirm the tests fail.** Run `uv run pytest tests/unit/test_screening_status.py tests/unit/test_prisma.py -v`. The failures must be import errors for the missing modules.
- [ ] **Implement `status.py` and `prisma.py`.** "Sought" means the papers whose S1 status is `included` or `uncertain`. "Not retrieved" means sought papers with no parse run (`paper_runs`). Draw the SVG in the PRISMA 2020 layout: four rows, identification boxes on the left, exclusion boxes on the right. Lay text out with fixed widths and wrap long reasons at 40 characters.
- [ ] **Add the CLI, routes and UI.** Add the CLI command, the routes and a **Review report** panel that shows the SVG inline (sanitised, since it is our own SVG) with a **Save** button. In `tests/ui/review.test.cjs`, test that the panel renders the counts and that Save calls the save route instead of using `<a download>`.
- [ ] **Run the tests.** Run `uv run pytest tests/unit/test_screening_status.py tests/unit/test_prisma.py tests/integration/test_screening_flow.py -v` and `node --test tests/ui/review.test.cjs`. All must pass. Commit.

### Task 2: Two-stage screening of borderline papers only

**Files:** Modify `src/litforge/screening/fulltext.py`, `src/litforge/analysis/claim_commands.py` (the `fulltext` command), `src/litforge/cli.py`, `app.js`, `docs/guide.md`. Test in `tests/unit/test_screening.py`.

**Interfaces:**

- **Thresholds:** `screening.borderline: tuple[float, float] = (0.4, 0.7)` in the config (`ScreeningConfig`), so the band can be tuned without a release. The S1 `score` is relevance plus inclusion coverage, not a calibrated confidence, so the band must be validated (last step).
- **Parameter:** `screen_fulltext(..., only: Literal["all","borderline"] = "all")` and `plan_calls(..., only=...)`. In borderline mode, `_work` keeps a paper only when its S1 decision is `uncertain`, or its S1 score lies in `[0.4, 0.7)`, and it has no human mark. Explicit `paper_ids` ignore the filter.
- **CLI:** `litforge screen --fulltext --borderline`. The full-text dialog gets a "Borderline only" checkbox, on by default when there are more than 50 S1 results.

Steps:

- [ ] **Write the failing test.** Write `test_fulltext_borderline_selects_uncertain_and_mid_scores`, seeded with S1 results `exclude 0.2`, `uncertain 0.3`, `include 0.55`, `include 0.9`, and a marked `uncertain`. Assert the selected IDs are exactly the second and third papers. Also write `test_fulltext_borderline_preview_counts_match_run`.
- [ ] **Confirm it fails.** Run `uv run pytest tests/unit/test_screening.py -k borderline -v`. It must fail with an unexpected-keyword error.
- [ ] **Implement the filter.** Implement it inside `_work` before the `top` slice, so `--top` still limits the borderline set. Record `only` in the run's `start_run` parameters so the run history shows the mode.
- [ ] **Validate the band on the gold benchmark.** Run `litforge eval screening` on the sbi10 gold benchmark in both modes with the local model. Pass condition: borderline mode misses no gold-included paper that all mode found. Record calls saved and recall in the commit message. If it fails, widen the band in the default config and repeat.
- [ ] **Run the tests and commit.** Run the test file and `tests/integration/test_screening_flow.py`, then commit.

### Task 3: Search-string builder

**Files:** Create `src/litforge/analysis/search_strategy.py`, `tests/unit/test_search_strategy.py`. Modify `config.py` (`TASK_NAMES` += `"search_strategy"`), `cli.py`, `review_routes.py`, `app.js` (a card under the criteria form), `docs/guide.md`, `docs/cli.md`.

**Interfaces:**

- **Databases:** `DATABASES = ("pubmed", "scopus", "wos", "arxiv", "openalex")`.
- **`SYNTAX`:** `SYNTAX: dict[str, DatabaseSyntax]` records, for each database, its allowed field tags (PubMed `[tiab]`, `[mh]`; Scopus `TITLE-ABS-KEY(...)`; WoS `TS=(...)`; arXiv `ti:`, `abs:`; OpenAlex plain search text), its operators and its wildcard character.
- **`build_strategy`:** `build_strategy(conn, gateway, project_id: str, databases: list[str], *, profile: str | None = None, approved: bool = False) -> Strategy`. `Strategy` has the fields `concepts: list[Concept(name: str, synonyms: list[str])]`, `queries: dict[str, str]` and `warnings: list[str]`.
- **Model output:** the model returns concepts only, with 2–5 concepts of 3–8 synonyms each. The query strings are assembled deterministically from `SYNTAX`, so their syntax is always valid. The model never writes database syntax.
- **`validate_query`:** `validate_query(db: str, q: str) -> list[str]` checks balanced parentheses and quotes and that only known field tags are used. It returns a list of problems.
- **CLI:** `litforge strategy -p NAME --db pubmed --db scopus [--preview] [--approve] [--json]`.
- **UI:** each string has a Copy button. The OpenAlex entry also gets **Search with it**. It does not depend on OpenAlex's Boolean support: it calls the existing `run_search` with a list of plain queries, one per combination of each concept's first two terms, at most 6 queries. `openalex_queries(s: Strategy) -> list[str]` is tested like the other assemblers.

Steps:

- [ ] **Write the failing tests.**
  - `test_assemble_queries_per_database`: given fixed concepts, assert the exact PubMed, Scopus and WoS strings. For example, the concepts `[("graph neural network", ["GNN","graph neural networks"]), ("epilepsy", ["seizure"])]` give, for PubMed, `("graph neural network"[tiab] OR GNN[tiab] OR "graph neural networks"[tiab]) AND (epilepsy[tiab] OR seizure[tiab])`.
  - `test_validate_query_flags_unbalanced_and_unknown_tag`.
  - `test_model_reply_with_extra_text_is_rejected`, using a fake gateway that returns 9 synonyms. Assert the list is trimmed to 8 and a warning is added.
  - `test_empty_criteria_raises`.
- [ ] **Confirm they fail.** Run `uv run pytest tests/unit/test_search_strategy.py -v`.
- [ ] **Implement the module.** Quote multi-word terms and leave single words bare. The prompt receives the research question and the criteria only; it never receives paper text. Cache the result through `start_run` and `finish_run`, keyed by `criteria_hash`, so reopening the card costs no call.
- [ ] **Run the tests and commit.** Run the tests and the UI test for the card's Copy and Search buttons, then commit.

### Task 4: RIS and BibTeX export with decisions

**Files:** Create `src/litforge/export/references.py`, `tests/unit/test_export_references.py`. Modify `src/litforge/writing/bib.py` (`bibtex_entry` gains `extra`), `cli.py`, `review_routes.py`, `app.js` (two buttons in the Export dialog), `docs/guide.md`.

**Interfaces:**

- **`export_references`:** `export_references(conn, project_id: str, fmt: Literal["ris","bib"], *, status: Collection[str] = ("included",), paper_ids: Collection[str] | None = None) -> str`.
- **RIS fields:** `TY JOUR`, `TI`, one `AU` per author from `authors_json`, `PY`, `DO`, `JO` from the venue, `AB`, `UR` from the OA URL, then `N1 LitForge: <status> (<stage>, <source>) — <reason>` and `KW litforge-<status>`. Each record ends with `ER  -`.
- **BibTeX:** reuse `writing.bib.bibtex_entry(conn, paper_id)`, so keys come from `citation_key` and match the Writing Studio. Add an optional `extra: dict[str, str] | None = None` parameter that appends fields before the closing brace. Pass `note` (the decision line) and `keywords` (`litforge-<status>`). Existing callers are unchanged.
- **CLI:** `litforge export-refs -p NAME --format ris|bib [--status included --status uncertain] [-o FILE]`. The default output is `<export folder>/references.ris` or `references.bib`.

Steps:

- [ ] **Write the failing tests.**
  - `test_ris_round_trip_keeps_doi_title_year`: export, then `parse_ris`, then compare the fields.
  - `test_bib_round_trip_with_braces_and_persian_title`.
  - `test_missing_fields_are_omitted_not_empty`.
  - `test_status_filter_uses_final_status`, which uses Task 1's `final_status`.
- [ ] **Confirm they fail.** Run `uv run pytest tests/unit/test_export_references.py -v`.
- [ ] **Implement the export.** Escape BibTeX special characters and leave Persian text as UTF-8.
- [ ] **Run the tests and commit.**

### Task 5: Obsidian links in the Markdown export

**Files:** Modify `src/litforge/export/markdown.py`, `cli.py` (`export --obsidian`), the export dialog in `app.js`. Test in `tests/unit/test_export_obsidian.py`.

**Interfaces:**

- **Parameter:** `export_markdown(..., obsidian: bool = False)`. When true, each note's YAML front matter gains `tags: [litforge/<status>]` and `aliases: [<title>]`.
- **Links:** a section `## Links in this export` lists `[[<slug>]]` for exported papers this paper cites or is cited by (`paper_relations`), plus papers sharing at least 3 references (`paper_references`).
- **Index:** `README.md` links each note as `[[<slug>]]` instead of a relative Markdown link.
- **Manifest:** the manifest format is unchanged, so cleanup of a previous export keeps working.

Steps:

- [ ] **Write the failing tests.** Write `test_obsidian_links_only_to_exported_notes` (a cited paper outside the selection gives no link), `test_obsidian_off_output_unchanged` (byte-identical to the current export for a fixture project) and `test_status_tag_in_front_matter`.
- [ ] **Confirm, implement, pass and commit.** Run `uv run pytest tests/unit/test_export_obsidian.py -v` and confirm it fails. Implement, run until it passes, then commit.

**Milestone A acceptance:** On a real project, the PRISMA numbers match a hand count from the review queue; a borderline S2 run costs fewer calls than the same `--top`; the RIS file imports into Zotero; the export folder opens as an Obsidian vault with working links.

## Milestone B: Evidence and integrations (package 2)

### Task 6: Criteria priorities and combined rules

**Files:** Modify `src/litforge/screening/criteria.py`, `decide.py`, `prompt.py`, `fulltext.py`, `ui/api.py` (`get_criteria`, `put_criteria`), the criteria form in `app.js`. Test in `tests/unit/test_screening.py` and `tests/ui/review.test.cjs`.

**Interfaces:**

- **Priorities:** `Criteria.priority: dict[str, Literal["critical","high","standard","low"]] = {}`, keyed by `I1`… and `E1`… (the numbering the prompt already uses). Missing keys mean `standard`.
- **Rules:** `Criteria.rules: list[str] = []` holds free-text rules such as "E2 applies only when I1 is met".
- **Hash compatibility:** `criteria_hash` and `fingerprint` must serialise exactly the old three fields when `priority` and `rules` are empty, so old projects keep their caches.
- **Decision rule:** `decide()` is changed as follows; everything else stays as it is.
  - A verified violated exclusion still excludes, unless its priority is `low` and a `critical` inclusion is met. In that case the paper is `uncertain`, with the reason "low-priority exclusion against a critical inclusion".
  - A `not_met` inclusion of priority `low` no longer blocks `include`.
  - In the score, inclusions are weighted `critical` 3, `high` 2, `standard` 1, `low` 0.5.
- **Prompt:** rules are listed in the prompt after the criteria as `Rules (apply them when answering):`. Projects with rules or priorities use `PROMPT_VERSION = "screen-v3"`. Projects without them keep `screen-v2`, so their caches stay valid.

Steps:

- [ ] **Write the failing tests.**
  - `test_criteria_hash_unchanged_without_priorities`, using a hash value computed on the current `main` and pasted into the test.
  - `test_low_exclusion_vs_critical_inclusion_is_uncertain`.
  - `test_low_inclusion_not_met_does_not_block_include`.
  - `test_weighted_score`, where `[met critical, not_met standard]` gives `0.5*rel/5 + 0.5*3/4`.
  - `test_rules_in_prompt_and_v3_only_when_set`.
- [ ] **Confirm they fail.** Run `uv run pytest tests/unit/test_screening.py -v`.
- [ ] **Record a baseline.** Before changing `decide`, run `litforge eval screening` on sbi10 and save the result. After the change, run it again with no priorities set; the decisions must be identical.
- [ ] **Implement the changes.** Implement them in the files above and apply the same rule to the S2 path, which reuses `decide`. In the form, each criterion line gets a small priority selector, and a "Rules" text area appears under the criteria.
- [ ] **Run the tests and commit.** Run the screening unit and integration tests and the UI test, then commit.

### Task 7: Check a paragraph's claims against the library

**Files:** Create `src/litforge/writing/claim_check.py`, `tests/unit/test_writing_claim_check.py`. Modify `config.py` (`TASK_NAMES` += `"write_check"`), `writing_routes.py`, the Writing Studio side panel in `app.js`, `tests/ui/writing.test.cjs`, `docs/guide.md`.

**Interfaces:**

- **`check_claims`:** `check_claims(conn, gateway, doc: Document, text: str, *, profile: str | None = None, approved: bool = False) -> ClaimCheck`.
- **Claims:** claims are the sentences from `writing.assist.sentences(text)` that contain no `\cite` yet, up to `MAX_CLAIMS = 5`. The model is not used to split the text.
- **Evidence:** for each claim, `retrieve_for_question(conn, search_query(conn, gateway, claim, sensitive=True), doc.project_id, k=6, max_per_paper=2)` is called, which translates Persian claims as Ask does. One `write_check` call per claim returns `[{evidence_id, relation: "supports"|"contradicts"|"unrelated", quote}]`.
- **Kept results:** a result is kept only when its `evidence_id` was sent and its quote is verified, using the same check as `fulltext_decision`.
- **Result type:** `ClaimCheck(claims: list[CheckedClaim(sentence: str, supports: list[Hit], contradicts: list[Hit])])`. `Hit` is `{evidence_id, paper_id, title, page, quote}`.
- **UI:** a **Check claims** button on the selection. For each sentence the panel shows the papers that support and contradict it. **Cite** calls the existing `cite()`, so the BibTeX file stays in sync. Nothing changes in the document until the user clicks Cite.

Steps:

- [ ] **Write the failing tests.** All use a fake gateway:
  - `test_contradicting_quote_is_kept_when_verified`;
  - `test_unverified_quote_is_dropped`;
  - `test_cited_sentences_are_skipped`;
  - `test_at_most_five_claims`;
  - `test_persian_claim_uses_query_translation`;
  - `test_preview_marks_text_sensitive`.
- [ ] **Confirm they fail.** Run `uv run pytest tests/unit/test_writing_claim_check.py -v`.
- [ ] **Implement it and run as a background job.** Run it as a job, like the other writing actions, with progress per claim.
- [ ] **Run the tests and commit.** Run the tests together with `tests/unit/test_writing_assist.py` and `node --test tests/ui/writing.test.cjs`, then commit.

### Task 8: Light bibliometrics (timeline and coupling clusters)

**Files:** Create `src/litforge/analysis/bibliometrics.py`, `tests/unit/test_bibliometrics.py`. Modify `cli.py`, `review_routes.py`, `app.js` (a Bibliometrics tab in the Review report panel).

**Interfaces:**

- **`timeline`:** `timeline(conn, project_id: str) -> list[YearRow(year: int, included: int, excluded: int, uncertain: int, unscreened: int)]`. It uses Task 1's `final_status`. Papers without a year are counted under `year=0` and drawn as "no year".
- **`coupling`:** `coupling(conn, project_id: str, *, status: Collection[str] = ("included",), min_shared: int = 2) -> list[Pair(a: str, b: str, shared: int, cosine: float)]`, where `cosine = shared / sqrt(|refs a| * |refs b|)` over `paper_references.ref_id`. This is bibliographic coupling. It needs no citing papers outside the library, which co-citation would.
- **`coupling_clusters`:** `coupling_clusters(pairs, threshold: float = 0.2) -> list[Cluster(paper_ids: list[str], top_refs: list[str])]` uses union-find over the pairs with `cosine >= threshold`. Clusters are listed with their most shared references, resolved to titles when those references are in the library.
- **Drawing:** the UI draws the timeline as a stacked SVG bar chart and the clusters as a list. No graph library is added.
- **CLI:** `litforge biblio -p NAME [--json]`.

Steps:

- [ ] **Write the failing tests.** Write `test_timeline_counts_by_final_status`, `test_coupling_cosine_exact` (papers with 4 and 9 references sharing 3 give `0.5`), `test_clusters_threshold` and `test_papers_without_references_are_ignored`.
- [ ] **Confirm, implement, pass and commit.** Run `uv run pytest tests/unit/test_bibliometrics.py -v` and confirm it fails. Implement, pass, add the UI and a UI test for the chart's numbers, then commit.

### Task 9: Two-way Zotero

**Files:** Create `src/litforge/discovery/zotero.py`, `tests/unit/test_zotero.py`, `docs/superpowers/specs/2026-10-23-zotero-probe.md`. Modify `cli.py`, `review_routes.py`, `app.js`, `docs/guide.md`.

**Interfaces:**

- **Base URL:** `ZOTERO = "http://127.0.0.1:23119"`.
- **Reading:** `collections() -> list[ZCollection(key: str, name: str, parent: str | None)]` and `collection_items(key: str) -> list[WorkRecord]` use the Zotero 7 local API (`/api/users/0/...`). The records are imported through the existing merge path with `source="zotero"`, so duplicates are matched as for any import. PDF attachments are copied only when their files are readable on disk.
- **Writing:** `push(records: list[WorkRecord], collection: str | None) -> PushReport(added: int, present: int, no_doi: int)` first lists the DOIs already in Zotero, then saves only missing items through the connector `POST /connector/saveItems`.
- **Errors:** every error becomes `ZoteroError` with one of these messages: "Zotero is not running", "Enable *Allow other applications on this computer to communicate with Zotero* in Zotero → Settings → Advanced", or the HTTP status.
- **CLI:** `litforge zotero collections`, `litforge zotero import -p NAME --collection KEY`, `litforge zotero push -p NAME [--status included] [--collection NAME]`.

Steps:

- [ ] **Run the probe and record what it finds.** Against a real Zotero 7, confirm that the local API reads collections and items, and that `saveItems` adds an item to the selected collection. Record the Zotero version, the exact request bodies and the replies in the spec file. If writing is not possible without a web API key, record that and limit the task to reading plus the RIS export from Task 4. Commit the probe report.
- [ ] **Write the failing tests.** They use a stdlib fake HTTP server on an ephemeral port:
  - `test_collections_tree`;
  - `test_items_to_workrecords_with_doi_and_authors`;
  - `test_push_skips_present_dois`;
  - `test_connection_refused_message`;
  - `test_403_message_names_the_setting`;
  - `test_never_contacts_non_loopback` (the base URL is fixed and not configurable from the API).
- [ ] **Confirm, implement, pass and commit.** Run `uv run pytest tests/unit/test_zotero.py -v` and confirm it fails. Implement, pass, run once against the real Zotero, then commit.

### Task 10: Translate non-English papers next to the original

**Files:** Create `src/litforge/storage/migrations/0033_paper_translations.sql` (Tasks 1–9 need no schema change; if another branch has taken 0033 by then, use the next free number), `src/litforge/analysis/paper_translate.py`, `tests/unit/test_paper_translate.py`. Modify `config.py` (`TASK_NAMES` += `"paper_translate"`), `screening/prompt.py`, `indexing/fts.py` (index the translation), the paper detail view in `app.js`, `cli.py`.

**Interfaces:**

- **Table:** `paper_translations(paper_id TEXT REFERENCES papers(id) ON DELETE CASCADE, field TEXT CHECK(field IN ('title','abstract')), lang TEXT, model TEXT, text TEXT, created_at TEXT, PRIMARY KEY(paper_id, field))`. The original columns are never changed.
- **`detect_language`:** `detect_language(text: str) -> str | None`. It is script based: Persian or Arabic letters give `fa` (or `ar` when no Persian-only letters appear), CJK gives `zh`, `ja` or `ko`, and Cyrillic gives `ru`. It returns `None` for English or when unsure. It needs no dependency.
- **`translate_papers`:** `translate_papers(conn, gateway, project_id: str, *, profile=None, approved=False, force=False) -> TranslateReport` translates the title and abstract of non-English papers to English, one local call per paper.
- **Screening:** the S1 prompt receives the original and, after it, `English translation (machine, for reading only): ...`. Quotes must come from the original and are verified against the original only, because a translation is not the paper's words. The fingerprint includes the translation, so a new translation rescreens the paper.
- **Search:** the translation feeds discovery and paper search (title and abstract matching) only. It never becomes an evidence span, so Ask and the claim check never quote it. If `indexing/fts.py` indexes only chunks, add a small `paper_translation_fts` table and merge its hits at paper level.
- **UI:** the paper detail shows a "Translated from Persian" note with a toggle to the original.
- **CLI:** `litforge translate-papers -p NAME [--preview] [--approve]`.
- **Full text:** full-text translation is out of scope for this task.

Steps:

- [ ] **Write the failing tests.**
  - `test_detect_language_cases`: Persian, Arabic, Chinese, Russian, English, mixed English with one Persian word giving `None`, and empty input.
  - `test_translation_kept_beside_original`.
  - `test_quote_from_translation_is_not_verified` (an English quote taken from the translation of a Persian abstract fails verification).
  - `test_translation_never_becomes_evidence_span`.
  - `test_english_query_finds_persian_paper_via_translation`.
  - `test_migration_upgrades_existing_db`.
- [ ] **Confirm they fail.** Run `uv run pytest tests/unit/test_paper_translate.py -v`.
- [ ] **Implement it.** Re-index FTS for the translated rows only, not the whole library.
- [ ] **Run the tests and commit.** Run the tests together with `test_screening.py`, `test_query_translate.py` and `test_db.py`, then commit.

**Milestone B acceptance:** On the user's Persian test project:

- **Claim check:** it finds at least one supporting paper for a known-true sentence, and no contradicting result appears without a verified quote.
- **Priorities:** they change only the projects that set them.
- **Zotero:** reading works against a real Zotero 7.
- **Translation:** an English Ask question finds a Persian paper through its translation.

## Verification for every task

- **Tests:** before each commit, run `uv run pytest`, `uv run ruff check .`, `uv run mypy` and `node --test tests/ui/`. Before starting, record a baseline so pre-existing failures are not attributed to this work.
- **Real model run:** features that call a model get one real run with the local model, following the feature-loop habit of three run/debug/fix rounds per feature. Fake gateways in the tests are not proof that a local model can follow the schema.
- **Codex:** Tasks 3, 4, 5 and 8 are well-scoped modules, so they are candidates for `codex exec --sandbox workspace-write`. Claude reviews the diff and runs the checks above before committing.

## Estimates and order

| Deliverable | Effort (one developer) |
| --- | --- |
| Task 1 (status + PRISMA) | 2–3 days |
| Tasks 2–5 | 1–2 days each |
| Milestone A | about 1.5–2 weeks |
| Tasks 6, 7 | 2–3 days each |
| Tasks 8, 10 | 2 days each |
| Task 9 | 2–4 days, depending on the probe |
| Milestone B | about 2–3 weeks |

**Order:** 1 → (2, 3, 4, 5 in any order) → 6 → 7 → 8 → 9 → 10. Task 1 comes first because Tasks 4, 5, 8 and the PRISMA counts depend on `final_status`.

**Relation to the desktop plan:** this plan is independent of it. If both run at the same time, land desktop Task 2, the server-lifecycle refactor, before this plan's first route change in `server.py`, and route all new endpoints through `review_routes.py`.
