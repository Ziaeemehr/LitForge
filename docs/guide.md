# LitForge guide

Details behind the [README](../README.md). Installation: [install.md](install.md).

## Command line

```bash
uv run litforge new my-review --topic "Attention mechanisms in deep learning"
uv run litforge add ~/papers
uv run litforge search "self-attention"
uv run litforge export
```

- `new` creates a project and makes it active, so later commands don't need `--project`.
- `add` imports, parses and indexes PDFs in one step. Run it again any time; nothing is processed twice.
- `search` returns the paper, section, page and a quoted snippet for every hit.
- `ask "your question" --evidence-only` retrieves evidence cards with stable IDs, section and page.
  It stores full chunk snapshots and an analysis run; reindexing preserves the evidence. Add `--json`
  for the run ID and spans, `-k 8` to set the number of cards, or `-p NAME` to choose a project.
  Model-written answers arrive in phase 1.
- `export` writes Markdown notes to `~/Downloads/LitForge/my-review/markdown/` and each paper's PDF under a readable name to `~/Downloads/LitForge/my-review/pdfs/` (set `LITFORGE_EXPORT_ROOT` to change the base folder, or pick a folder per project in the web UI); `--only relevant` keeps just the papers marked ✓ (`--only not-irrelevant` drops the ✗ ones).

## Relevance, importance and review priority

Every result gets two scores, both between 0 and 1:

- **Relevance** to your seed or query: meaning (embeddings), shared references, citation links,
  agreement between search channels.
- **Importance**, independent of your query: field- and year-normalized citation percentile
  (OpenAlex), how many papers in the same result set cite it (core literature), citations in the
  last two years, and the venue's 2-year mean citedness. Retracted papers get 0 and a warning.

**Review priority** = 0.65 × relevance + 0.35 × importance, the default sort order, so a large
result set can be read from the top.

## Screening with a local model

Relevance counts shared words and citations; it does not read the paper. Screening does: a local
model (Ollama, default `qwen3:4b`) reads the title and abstract of a run's top papers and checks them
against the project's research question and inclusion/exclusion criteria. It suggests **include**,
**uncertain** or **exclude** with a reason and quotes from the abstract (quotes not found in the
title or abstract are flagged). A violated exclusion criterion requires a verified quote to exclude;
otherwise the decision is uncertain with the reason “exclusion quote not verified”. The decision and a
0–1 score are computed by fixed rules, not by the model; a paper without an abstract is never excluded. Your ✓/✗ marks are separate and always decide.
Nothing leaves your computer.

1. `ollama pull qwen3:4b` once (`qwen3:8b` is slower but more careful: set `screening.model`).
2. In the web UI open a run, fill in **Screening** (question, criteria one per line), press **Screen**.
   About 2 s per paper on an RTX 5070 with `qwen3:4b`.
3. Sort by **screening**, filter by decision, **Mark top N ✓**, then export the ✓ papers.

Changing the criteria or the model marks earlier results as stale; screening again re-checks them,
and unchanged papers are not sent to the model twice.

### Model gateway and cloud policy

LitForge's model layer can also target an OpenAI-compatible endpoint, including a university-hosted
vLLM server. Cloud use is disabled by default. The gateway routes only explicit evidence packets,
never complete PDF bytes, and supports hard per-call, per-run, and monthly budgets. API keys are read from
environment variables named by the configuration; their values are not stored in the database or logs.
Named remote profiles can be added under `models.profiles`; each profile declares its endpoint, model,
and API-key environment-variable name. No remote profile is configured by default.

The policies are local_only, ask_before_cloud, and hybrid. Remote fallback is never silent:
sensitive evidence and ask_before_cloud require explicit approval from the calling feature. The current
screening UI continues to use Ollama by default; cloud-backed research features will opt into the
gateway in later phases.

Remote profiles need `pricing.input_per_million_usd` and `pricing.output_per_million_usd` before
the gateway can dispatch. `litforge calls --limit 20` shows the audit log; `--run RUN_ID` filters
one analysis run, and `--json` includes the recorded fields.

### Measure retrieval

```bash
uv run litforge eval retrieval configs/benchmark.example.yaml -k 10 --json
```

Copy [benchmark.example.yaml](../configs/benchmark.example.yaml) and label questions against your
own indexed papers. Use a paper ID, DOI or arXiv ID; optionally require a PDF page and a substring
of the evidence text. Each question needs a unique ID and at least one target. YAML and JSON both
work. A useful first benchmark has 30 questions from your reading, with the pages and passages you
would expect to find; the example uses synthetic test papers.

The command stores a run and reports mean Recall@k (fraction of targets found per question),
hit-rate@k (questions with any target found), MRR (reciprocal rank of the first relevant hit), and
missed targets. Retrieval currently uses lexical OR search over content words and quoted phrases.
Keep the same benchmark when comparing future retrieval methods.

## Find papers

```bash
uv run litforge discover "self-attention"            # search OpenAlex, Crossref and arXiv
uv run litforge discover --seed 10.48550/arXiv.1706.03762   # follow references and citing papers
uv run litforge import refs.bib                      # add a .bib, .ris or .csv reference list
uv run litforge enrich                               # fill in missing DOI, year, venue, abstract
uv run litforge papers --no-pdf                      # ranked list of papers still missing a PDF
uv run litforge fetch                                # download open-access PDFs of the ✓ papers
```

`fetch` downloads a legal open-access copy (arXiv, or the publisher's open-access URL; on a
landing page it follows the `citation_pdf_url` link), attaches it to the existing record, then
parses and indexes it like `add`, so exports contain the full text. `--only not-irrelevant` or
`--only all` widens the selection. Paywalled papers are reported and skipped; add their PDFs with
`add`.

### Papers similar to a paper you have

```bash
uv run litforge similar 10.7554/eLife.56261           # by DOI (or arXiv id, or a PDF file)
uv run litforge similar --abstract-file abstract.txt  # from an abstract only
```

Candidates come from the seed's references, papers citing it, OpenAlex related works and
searches built from its key phrases (for an abstract-only seed, also the citations of the closest
matches). They are ranked by text similarity, shared references and citation links, and each
result says why it was found. Meaning-based similarity uses a small embedding model that runs on
a laptop CPU (`uv sync --extra embed`; downloaded once to `data/cache/models`). On a GPU machine
set `similar.embedding_model` (e.g. `BAAI/bge-large-en-v1.5`) and `embedding_device: cuda` in the
config.

Results from all sources are merged (by DOI, arXiv id, OpenAlex id, then title and year),
ranked by relevance, and added to the active project. Every run is recorded together with why
each paper was found. When you later `add` the PDF of a discovered paper, it is attached to the
existing record. `discover` without a query uses the project topic.

API responses are cached for a week in `data/cache/http`. OpenAlex meters usage: without an API
key there is a small free daily budget (about 100 searches; single-paper lookups are free). Set
`LITFORGE_OPENALEX_API_KEY` to use your own key and `LITFORGE_MAILTO` to identify yourself to
OpenAlex and Crossref (both optional).

Other useful commands:

```bash
uv run litforge status            # what is in the active project
uv run litforge project list      # all projects (* marks the active one)
uv run litforge project use NAME  # switch the active project
uv run litforge doctor            # check the environment
uv run litforge screen --top 100  # screen the latest run's top papers (criteria are set in the web UI)
```

Any command accepts `--project NAME` to work on a project other than the active one.
`litforge --help` lists everything, including the individual `ingest`, `parse` and `index`
steps that `add` combines.

## Where things are stored

All data lives in `./data` (git-ignored). Use `--data-dir PATH` or `LITFORGE_HOME=PATH` to move it.

```text
data/
├── library/              shared by all projects
│   ├── papers.sqlite     metadata, sections, search index
│   ├── pdfs/             original PDFs, named by SHA256 (never deleted)
│   └── parsed/           per-PDF output: canonical.json, paper.md, assets/
└── projects/<name>/      project settings
```

## How a PDF is processed

1. **Import:** the file is hashed; identical files are stored once. Corrupt or encrypted files
   are kept but marked `quarantined`.
2. **Parse:** PyMuPDF extracts text with page numbers and positions, restores two-column reading
   order, removes running headers, and detects sections, captions, figures, tables and display
   equations (saved as image crops). A quality check marks papers that need a stronger parser
   (`needs_docling`) or OCR (`needs_ocr`).
3. **Index:** text is split into section-aware chunks and indexed with SQLite FTS5.

Optional: `uv sync --extra docling` installs [Docling](https://github.com/docling-project/docling)
as a fallback for complex layouts.

## Better math (equations and tables)

The text layer of a PDF keeps prose well but scatters display equations into single characters, and
in scanned papers often misses them. Pages that show it (an equation, a table, an equation number,
fragment lines) can be rewritten at two levels, before exporting (**Math** menu in the web UI) or with
`litforge enhance --level medium|high`:

| Level | How | Speed | Needs |
|---|---|---|---|
| medium | [Pix2Text](https://github.com/breezedeus/Pix2Text) finds every formula (inline and displayed) and reads it as LaTeX; the text stays as extracted | ~1.5 s/page on a GPU, ~5 s on a CPU | `uv sync --extra formulas-gpu` (NVIDIA) or `--extra formulas` (CPU) |
| high | a local vision model (Ollama, `qwen2.5vl:7b`) rewrites whole pages: formulas, tables and text, also in scanned papers; a transcription that does not match the page's text is rejected | ~12 s/page on a GPU, about a minute without | `ollama pull qwen2.5vl:7b` (8 GB of memory); the page offers to install Ollama and the model |

Results are kept per page, so a later export or a higher level only processes what is new. Each
note's front matter says how its equations were produced (`math: raw | medium | high`), so a
language model reading the notes knows how far to trust them: a sensible workflow is to screen
many papers with plain exports and rewrite only the ones you read closely.

On 31 pages of 12 arXiv papers, scored against the papers' own LaTeX, both levels read about 95% of
each displayed equation correctly (character similarity), Pix2Text finding a few more and the vision
model getting more exactly right; Nougat and UniMERNet did no better and were slower.

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

Benchmark the parser on a set of open-access arXiv papers:

```bash
uv run python scripts/fetch_benchmark.py     # downloads PDFs to data/benchmark/pdfs
uv run python scripts/benchmark_parsers.py   # writes data/benchmark/parser_benchmark.csv
```

To keep the virtual environment outside the repository, set `UV_PROJECT_ENVIRONMENT`
(for example `export UV_PROJECT_ENVIRONMENT=$HOME/envs/litforge`) before running `uv`.

## Status and limitations

Implemented: import, parsing, search, Markdown export, paper discovery (OpenAlex,
Crossref, arXiv, citation chaining, reference-list import) and title/abstract screening with a
local model. Planned: full-text screening and deep analysis of selected papers.

Known limitations of the fast parser:

- Tables drawn without vertical rules get a caption and crop but no cell grid (their text is
  still searchable).
- Without [better math](#better-math-equations-and-tables), equation text is approximate; the saved
  image crop is the reliable version.
- Some figures cannot be paired with their caption and get no crop.

Design documents (in Persian and English) are in this folder.

## License note

PyMuPDF is AGPL-licensed. Review this before distributing LitForge or derived work.

## Evidence answers and study matrices (CLI)

`litforge ask "How were neural mass parameters inferred?"` retrieves evidence, generates at
most eight cited sentences, then audits each cited sentence. Use `--no-audit` to omit the
second pass, `--audit-profile NAME` for a different auditor, or `--evidence-only` for the
original retrieval-only cards. `--json` includes sentence flags, audit verdicts and provenance.
All model calls use the configured gateway. Source text is treated as untrusted data;
unknown citations are removed and uncited facts become `unsupported`. Numeric checks compare
normalised numbers (including decimal, scientific and thousands notation) with the cited
text actually sent to the model. These checks validate citations and numbers, not semantic truth.

```bash
litforge ask "How were parameters inferred?" --profile local --preview
litforge ask "How were parameters inferred?" --sensitive --json
litforge audit ANSWER_RUN_ID --profile local
litforge review --json
litforge review accept SENTENCE_OR_CELL_ID --reason "checked the source"
litforge review reject SENTENCE_OR_CELL_ID --reason "incorrect extraction"
litforge review edit CELL_ID '3.25' --reason "corrected from the table"
litforge brief ANSWER_RUN_ID --matrix studies -o brief.md

litforge matrix create studies --question "How were neural models evaluated?" --columns configs/matrix.example.yaml
litforge matrix extract studies --preview
litforge matrix extract studies
litforge matrix add-column studies method --label Method --instruction "Describe the inference method" --type text
litforge matrix extract studies  # only new/changed cells; --force re-extracts
litforge matrix show studies
litforge matrix export studies --format csv -o studies.csv
litforge matrix list
```

Commands use the active project, or `-p NAME`. Matrix names are unique within a project;
IDs can also identify matrices. `matrix extract --paper ID` can be repeated to select rows.
Cells distinguish `reported`, `not_reported`, `not_found`, `not_applicable`, and `error`.
Markdown exports include evidence footnotes and unit/retrieval warnings; CSV includes a status
and evidence column for every field; JSON includes full provenance. Extraction run results
retain historical model values. Human cell overrides persist through cache reuse and forced
extraction; similar cells are never changed automatically. Fresh model extractions reopen previous
accept/reject decisions unless a human value override is present. Human review history is append-only.
Number/boolean/text edits must match the column type, and categorical edits must match an option.
When multiple artifacts have current parses for a paper, extraction uses the newest parse.
Cached errors require `--force` to retry, like other unchanged cells.

`ask`, `audit` and `matrix extract` accept `--profile NAME`, `--approve`, and `--preview`.
Preview never dispatches a model call and exits 0. A route requiring cloud approval prints the
plan and exits 3 unless `--approve` is supplied. Plans show routes, evidence counts, estimated
tokens, maximum costs and blocked reasons. For automatic audit, the ask preview conservatively
includes up to eight audit calls; actual calls depend on the generated sentences. Matrix runs
stop dispatching when the run budget is exhausted and mark remaining cells `error: budget`.
Use `--sensitive` on ask to retain sensitive-content approval requirements during later audits.

The retrieval benchmark YAML can also contain `matrix:` targets with `paper`, `column`,
`status`, and optional `value`. `litforge eval matrix BENCHMARK --matrix studies` reports status
accuracy, value accuracy (numbers within 1% relative; text case-insensitive exact/contained),
and whether cited spans belong to their row's paper. `litforge eval answers BENCHMARK --profile
local` reports citation precision against relevant targets (invalid IDs count as incorrect citations),
unsupported-sentence rate, and invalid citation count. Both accept `--json` and store analysis runs. Briefs refuse any factual sentence
without evidence or an explicit unsupported/interpretation label.


### Evidence Lens in the web UI

Select a project, then open **Ask** to ask a question about its library evidence. Sentence rows show
kind, validation flags and audit verdicts; citation chips open the quote, paper, section and PDF page
in the drawer. **Run audit** checks the stored answer with the chosen profile. The **Audit** filter
shows weak or single-source sentences; **Answer · Evidence · Needs review** switches between the
answer, source cards and the project's open reviews. Accept, reject or edit a sentence or cell in the drawer.

Open **Matrix** to create a comparison with named, typed columns, extract values, inspect cells and
export Markdown, CSV or JSON. Add columns to an existing matrix and extract again to fill only new
or changed cells. Unit and retrieval warnings appear above the table. Number and boolean edits use
numbers and `true`/`false`; categorical edits use an exact option. A blank profile uses the configured
local default. Every remote action shows provider, model, evidence count, tokens and maximum cost
before confirmation. Press **?** for the guide and **Esc** to close the drawer or a dialog.

## Claims, full-text screening and review queue

```bash
litforge claims extract -p my-review --profile local --preview
litforge claims extract -p my-review --paper PAPER_ID  # repeat --paper to select papers
litforge claims cluster -p my-review
litforge claims relate -p my-review --max-pairs 200
litforge claims show -p my-review --cluster CLUSTER_ID --json
litforge claims landscape -p my-review
litforge claims review accept CLAIM_ID
litforge claims review reject CLAIM_ID --reason "source disagrees"
litforge claims review edit CLAIM_ID --text "Corrected finding" --reason "checked source"
litforge screen --fulltext -p my-review --top 100
litforge queue -p my-review -n 20 --json
litforge brief ANSWER_RUN_ID --claims -o brief.md
litforge eval screening benchmark.yaml -p my-review --json
```

Claim extraction uses result/discussion/conclusion/abstract sections, with project-question FTS
retrieval as fallback, then the first chunks of the paper (body before front matter, up to eight).
Papers without usable text are counted as `no_text` in the extraction report and CLI output.
Each paper yields at most eight cited claims; unknown IDs are removed,
uncited claims dropped, identical claims within a paper deduplicated. Cache keys include the current
parse, provider, model and prompt. `--force` re-extracts while preserving human reviews and edits.
Clustering uses TF-IDF with an outcome-match boost and links only claims from different papers.
Relations require model assessment; polarity rules only prioritise candidate contradictions.
All possible causes are labelled hypotheses; missing citations additionally flag an unsupported hypothesis.
The landscape displays distinct-paper evidence strength separately from citation popularity.

S2 retrieves up to three chunks per criterion and makes one call per parsed paper. A violated exclusion
must cite an exact quote inside its own transmitted evidence span (whitespace may vary); otherwise its
exclude suggestion becomes uncertain. Unparsed papers remain S1 and appear in `needs_pdf`.
S1 suggestions, S2 decisions and human ✓/✗ changes append reasons and criteria versions to immutable
screening history. S1 also requires a verified exclusion quote and keeps the missing-abstract rule. Its
`screen-v2` prompt adds an untrusted-text instruction, so earlier cached S1 results are screened again.
Marks remain human decisions.
The queue only ranks unmarked papers; it never changes marks or model decisions.

In the UI, **Claims** lists clusters, claims, relations, hypotheses and reusable evidence cards, with
accept/reject controls. Results show **S2** badges, nearby-mark relevance predictions and a **Review queue**
sort with reason tooltips. Paper details include S2 evidence and screening history. Claims and full-text
screening offer model-profile selectors and the same remote-preview confirmation as Ask/Matrix.
CLI model commands accept `--profile`, `--approve` and `--preview`; preview makes no model calls.
Budget exhaustion preserves completed work and reports `budget_stopped`.

Benchmarks may contain only screening labels:

```yaml
name: screening-check
screening:
  - paper: PAPER_ID  # also accepts a DOI or arXiv ID
    relevant: true
```

Evaluation reads stored S1/S2 results, reports each stage's false-negative, exclusion and uncertain rates
and shortlist recall, and saves an analysis run. Exit code 1 means a false-negative rate exceeds
`screening.max_false_negative_rate` (default `0.05`). Missing stage results count as uncertain and stay
on the shortlist; the report also shows how many papers were screened.

Implementation choices where the design leaves a formula or representation open:

- Human marks use stage S3; clearing a mark records uncertain with reason “Mark cleared”.
- With no model score, uncertainty is 1. Coverage is the mean of the missing-parse and missing-abstract
  indicators. Boundary similarity averages the five closest marks on each side; kNN uses the five closest
  overall with similarity weights, returning no prediction when all similarities are zero. Exact ties
  predict 0.5 and do not constitute disagreement. Queue ties use paper ID.
- Distinct papers stand in for independence. Similarity alone never proves support or replication: counts
  use explicit relations, with the earliest dated paper as origin (unknown years sort last, then paper ID).
  Contradicting relations attribute the challenge to the later paper; replication counts distinct later
  papers with a replication relation. Contradicting papers are removed from the supporting count.
- S2 criterion evidence is `{evidence_id, quote}`; IDs outside the transmitted packet are removed and
  counted as `invalid_citations`. S1 verifies quotes against title and abstract; S2 requires an exact
  quote in the cited span (allowing whitespace differences). Editing a claim invalidates its model relations until
  reassessed. Project deletion preserves append-only screening history and human review records.
