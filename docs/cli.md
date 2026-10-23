# Command line

Every LitForge feature is also available from the terminal. In an installed copy run commands from the
install folder with `uv run litforge …`; with an activated environment just `litforge …`.
Global options come before the command, e.g. `litforge --data-dir ~/research/data status`.
Add `--help` to any command for the same information.

This page is generated from the program itself (`python scripts/gen_cli_docs.py`), so it matches the installed
version.

## Global options

| Option | Meaning |
|---|---|
| `--data-dir` | Data root (default $LITFORGE_HOME or ./data) |
| `--config` | YAML overriding configs/default.yaml |
| `--install-completion` | Install completion for the current shell. |
| `--show-completion` | Show completion for the current shell, to copy it or customize the installation. |

## Projects and library

### `litforge new`

Create a project and make it active (shortcut for `project create`).

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--topic` | Short topic description |

### `litforge project`

Manage project workspaces.

#### `litforge project create`

Create a project and make it active.

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--topic` | Short topic description |
| `--question` | Research question |

#### `litforge project delete`

Delete a project (papers and PDFs stay in the library; exports are kept unless --delete-exports).

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--yes`, `-y` | Don't ask for confirmation |
| `--delete-exports` | Also delete its exported notes and PDFs |

#### `litforge project list`

List projects with paper counts (* = active).

#### `litforge project use`

Make an existing project active, so --project can be omitted.

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |

### `litforge library`

Manage papers in the shared library.

#### `litforge library remove-unused`

Permanently remove papers that belong to no project and have no cited evidence.

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Don't ask for confirmation |
| `--paper` | Paper ID (repeatable; default: all unused) |

### `litforge status`

Summarize library or project state.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |

### `litforge papers`

List the project's papers, best discovery score first.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |
| `--no-pdf` | Only papers without a PDF |
| `--limit`, `-n` |  |
| `--json` |  |

### `litforge doctor`

Check Python, parsers, SQLite FTS5, GPU, Ollama, disk and schema.

| Option | Meaning |
|---|---|
| `--json` |  |

## Adding papers

### `litforge ingest`

Import PDFs into the library (idempotent: identical files are never duplicated).

| Option | Meaning |
|---|---|
| `SOURCES` | argument (required) |
| `--project`, `-p` | Project (default: the active project) |
| `--dry-run` |  |
| `--json` |  |

### `litforge add`

Add PDFs to a project: import, parse and index in one step (safe to re-run).

| Option | Meaning |
|---|---|
| `SOURCES` | argument (required) |
| `--project`, `-p` | Project (default: the active project) |

### `litforge import`

Import a reference list into the project (papers without PDFs are kept as records).

| Option | Meaning |
|---|---|
| `FILE` | argument (required) |
| `--project`, `-p` | Project (default: the active project) |

### `litforge discover`

Find papers by search queries and/or by following citations of seed papers.

| Option | Meaning |
|---|---|
| `QUERIES` | argument |
| `--project`, `-p` | Project (default: the active project) |
| `--source`, `-s` | openalex, crossref, arxiv |
| `--limit`, `-n` | Results per source and query |
| `--seed` | DOI, arXiv id or paper id to follow citations from |
| `--citations` | With --seed: references, cited_by or both |
| `--top` | How many ranked results to show |
| `--refresh` | Ignore cached API responses |
| `--json` |  |

### `litforge similar`

Find papers similar to seed papers (citations, related works, key phrases, text similarity).

| Option | Meaning |
|---|---|
| `SEEDS` | argument |
| `--title` | Seed paper title |
| `--abstract` | Seed abstract text |
| `--abstract-file` | Text file with the seed abstract |
| `--project`, `-p` | Project (default: the active project) |
| `--top` | How many ranked results to show |
| `--no-embeddings` | Rank by TF-IDF only |
| `--refresh` | Ignore cached API responses |
| `--json` |  |

### `litforge enrich`

Complete missing DOI, year, venue and abstract from OpenAlex/arXiv; verify DOIs.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |
| `--refresh` |  |

### `litforge fetch`

Download open-access PDFs (arXiv, publisher OA copies), then parse and index them.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |
| `--only` | Which papers: relevant (marked ✓), not-irrelevant (all but ✗) or all |

## Reading PDFs

### `litforge parse`

Parse PDFs or install the accurate parser.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |
| `--force` | Re-parse even when cached |
| `--no-docling` | Disable structural fallback |
| `--parser` |  |
| `--paper` | Paper ID (repeatable) |
| `--dry-run` |  |
| `--json` |  |

#### `litforge parse install-mineru`

| Option | Meaning |
|---|---|
| `--yes`, `-y` |  |

### `litforge index`

Chunk current parses and build the FTS5 index.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |
| `--force` |  |
| `--json` |  |

### `litforge enhance`

Better math: rewrite pages with equations, tables or broken text (the next export uses them).

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |
| `--only` | Which papers: relevant (marked ✓), not-irrelevant (all but ✗) or all |
| `--level` | medium: Pix2Text reads the formulas, text from the PDF (fast; GPU if installed). high: the local vision model rewrites whole pages (best; also fixes scanned text) |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |

### `litforge versions`

Inspect and manage preprint, accepted and published versions.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |

#### `litforge versions confirm`

Confirm that two paper records are versions of one work.

| Option | Meaning |
|---|---|
| `A` | argument (required) |
| `B` | argument (required) |

#### `litforge versions link`

Link explicit registry versions and record title-based suggestions.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |

#### `litforge versions reject`

Reject and remember a suggested version link.

| Option | Meaning |
|---|---|
| `A` | argument (required) |
| `B` | argument (required) |

#### `litforge versions set`

Override the version label on a paper's current PDF.

| Option | Meaning |
|---|---|
| `PAPER` | argument (required) |
| `--version` |  |

#### `litforge versions set-primary`

Choose a paper as the primary version used by project analysis.

| Option | Meaning |
|---|---|
| `PAPER` | argument (required) |

#### `litforge versions unlink`

Remove a paper from its version group and remember that it is a different work.

| Option | Meaning |
|---|---|
| `PAPER` | argument (required) |

### `litforge asset`

Analyze selected table, figure and equation crops.

#### `litforge asset analyze`

Analyze one asset crop with the vision model; values read from images stay labelled as such.

| Option | Meaning |
|---|---|
| `ASSET_ID` | argument (required) |
| `--mode` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |
| `--json` | Print JSON instead of text |

### `litforge glossary`

Equation symbols grouped by meaning across the project's analyzed equations.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--json` | Print JSON instead of text |

### `litforge tools`

Check and install optional LitForge tools.

#### `litforge tools install`

Install optional tools.

#### `litforge tools install mineru`

Install MinerU for accurate PDF parsing (~4 GB).

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Don't ask for confirmation |

#### `litforge tools install nemotron`

Set up Nemotron Parse on an NVIDIA GPU: two small packages and the model (~2.5 GB).

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Don't ask for confirmation |

#### `litforge tools install pix2text`

Install Pix2Text for Math: medium (~2 GB).

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Don't ask for confirmation |

#### `litforge tools install tectonic`

Install Tectonic, the LaTeX engine of the Writing Studio (~22 MB, packages download on first use).

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Don't ask for confirmation |

## Searching and screening

### `litforge search`

Lexical (BM25) search; every hit carries paper, section, page and a quoted snippet.

| Option | Meaning |
|---|---|
| `QUERY` | argument (required) |
| `--project`, `-p` | Project (default: the active project) |
| `--limit`, `-n` |  |
| `--max-per-paper` | Cap hits per paper |
| `--raw` | Pass query as raw FTS5 syntax |
| `--json` |  |

### `litforge screen`

Screen a run's top papers against the project's criteria with a local model (set criteria in the web UI).

| Option | Meaning |
|---|---|
| `--run` | Run id (default: the project's latest run) |
| `--top` | How many top papers of the run to screen |
| `--model` | Ollama model (default: screening.model) |
| `--fulltext` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |
| `--project`, `-p` | Project (default: the active project) |
| `--json` |  |

### `litforge queue`

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `-n` |  |
| `--json` |  |

## Analysis

### `litforge ask`

Answer a research question with evidence citations and an optional audit.

| Option | Meaning |
|---|---|
| `QUESTION` | argument (required) |
| `--evidence-only` |  |
| `--project`, `-p` | Project (default: the active project) |
| `-k` |  |
| `--json` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |
| `--audit`, `--no-audit` |  |
| `--audit-profile` |  |
| `--sensitive` |  |
| `--all-versions` | Use non-primary versions too |

### `litforge audit`

Audit a stored answer against its cited spans.

| Option | Meaning |
|---|---|
| `RUN_ID` | argument (required) |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |

### `litforge matrix`

Create, extract and export study matrices.

#### `litforge matrix add-column`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `KEY` | argument (required) |
| `--label` |  |
| `--instruction` |  |
| `--type` |  |
| `--unit` |  |
| `--options` |  |
| `--parameter` |  |
| `--sensitivity-analysis` |  |
| `--sensitivity-for` | Parameter column key this sensitivity column tests (repeatable) |
| `--project`, `-p` |  |

#### `litforge matrix create`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--question` |  |
| `--columns` |  |
| `--project`, `-p` |  |

#### `litforge matrix export`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--format` |  |
| `-o`, `--out` |  |
| `--project`, `-p` |  |

#### `litforge matrix extract`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--paper` |  |
| `--force` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |
| `--project`, `-p` |  |

#### `litforge matrix list`

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |

#### `litforge matrix show`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--project`, `-p` |  |

### `litforge claims`

Extract, connect and review cited claims.

#### `litforge claims cluster`

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--threshold` |  |

#### `litforge claims extract`

| Option | Meaning |
|---|---|
| `--paper` |  |
| `--project`, `-p` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |
| `--force` |  |

#### `litforge claims landscape`

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--json` |  |

#### `litforge claims relate`

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--max-pairs` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |

#### `litforge claims review`

| Option | Meaning |
|---|---|
| `ACTION` | argument (required) |
| `ID` | argument (required) |
| `VALUE` | argument |
| `--text` |  |
| `--reason` |  |

#### `litforge claims show`

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--cluster` |  |
| `--json` |  |

### `litforge gaps`

Design and review research proposals from the evidence landscape.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--json` | Print JSON instead of text |

#### `litforge gaps design`

Draft a research-proposal card for a gap; it must be reviewed before export.

| Option | Meaning |
|---|---|
| `GAP_ID` | argument (required) |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |
| `--json` | Print JSON instead of text |

#### `litforge gaps export`

| Option | Meaning |
|---|---|
| `CARD_ID` | argument (required) |
| `--out`, `-o` |  |

#### `litforge gaps review`

Accept or reject a proposal card; only accepted cards can be exported.

| Option | Meaning |
|---|---|
| `CARD_ID` | argument (required) |
| `ACTION` | argument (required) |
| `--reason` |  |
| `--json` | Print JSON instead of text |

### `litforge repro`

Extract the built-in reproducibility matrix and write cited reproduction briefs.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--paper` |  |
| `--md` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |

### `litforge brief`

Write a cited research brief with method and evidence appendix.

| Option | Meaning |
|---|---|
| `RUN_ID` | argument (required) |
| `--matrix` |  |
| `-o`, `--out` |  |
| `--claims` |  |

### `litforge do`

Plan, preview and execute a request using the closed action registry.

| Option | Meaning |
|---|---|
| `REQUEST` | argument (required) |
| `--project`, `-p` |  |
| `--yes`, `-y` |  |
| `--approve-cloud` | Separately approve cloud model routes |
| `--json` | Print the preview and run as JSON |

### `litforge console`

Preview, execute and resume research workflows.

#### `litforge console cancel`

Request cancellation between steps of a running console execution.

| Option | Meaning |
|---|---|
| `RUN_ID` | argument (required) |

#### `litforge console resume`

Resume a failed or cancelled console run from its next checkpoint.

| Option | Meaning |
|---|---|
| `RUN_ID` | argument (required) |
| `--yes`, `-y` |  |
| `--approve-cloud` | Separately approve cloud model routes |

#### `litforge console runs`

List checkpointed console runs.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |

### `litforge eval`

Measure LitForge against labelled gold benchmarks.

#### `litforge eval all`

Run every part of a gold benchmark and write a Markdown report.

| Option | Meaning |
|---|---|
| `BENCHMARK` | argument (required) |
| `--project`, `-p` |  |
| `--profile` |  |
| `--approve` |  |
| `--preview` |  |
| `--skip-models` | Labels and retrieval only |
| `--force` | Redo cached cells, decisions and claims |
| `-k` |  |
| `-o`, `--out` |  |
| `--json` |  |

#### `litforge eval answers`

Measure citation precision and unsupported-sentence rate.

| Option | Meaning |
|---|---|
| `BENCHMARK` | argument (required) |
| `--profile` |  |
| `--approve` |  |
| `--project`, `-p` |  |
| `--json` |  |

#### `litforge eval check`

Confirm every labelled quote is on its page in the indexed text.

| Option | Meaning |
|---|---|
| `BENCHMARK` | argument (required) |
| `--project`, `-p` |  |
| `--json` |  |

#### `litforge eval claims`

Measure claim recall and relation accuracy against labelled claims.

| Option | Meaning |
|---|---|
| `BENCHMARK` | argument (required) |
| `--project`, `-p` |  |
| `--json` |  |

#### `litforge eval matrix`

Measure matrix status/value accuracy and citation validity.

| Option | Meaning |
|---|---|
| `BENCHMARK` | argument (required) |
| `--matrix` |  |
| `--project`, `-p` |  |
| `--json` |  |

#### `litforge eval retrieval`

Measure Recall@k, hit-rate@k and MRR against labelled evidence.

| Option | Meaning |
|---|---|
| `BENCHMARK` | argument (required) |
| `--project`, `-p` | Project (default: the active project) |
| `-k` |  |
| `--json` |  |

#### `litforge eval screening`

| Option | Meaning |
|---|---|
| `BENCHMARK` | argument (required) |
| `--project`, `-p` |  |
| `--json` |  |

## Living review

### `litforge refresh`

Refresh stored searches and print evidence-change signals.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--no-screen` |  |
| `--no-extract` |  |
| `--no-claims` |  |
| `--notify` |  |
| `--approve` |  |

### `litforge snapshot`

Take an immutable snapshot of the current project.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--label` |  |

### `litforge snapshots`

List snapshots, newest first.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |

### `litforge delta`

Compare two snapshots and optionally write Markdown.

| Option | Meaning |
|---|---|
| `OLD` | argument (required) |
| `NEW` | argument (required) |
| `--md` |  |

### `litforge watch`

Explicitly write or remove a living-review schedule.

#### `litforge watch install`

Write schedule files; print the explicit OS activation command.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--every` |  |

#### `litforge watch remove`

Remove schedule files; print the explicit OS deactivation command.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |

## Export

### `litforge export`

Export a project's papers as Markdown with relative asset links.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |
| `--format` | Export format (markdown or bibtex) |
| `--only` | Which papers: all, relevant (marked ✓) or not-irrelevant (all but ✗) |
| `--fetch` | First download open-access PDFs of these papers (full-text notes) |
| `--all-versions` | Export non-primary versions too |

## Models

### `litforge models`

Choose and test model backends.

#### `litforge models add`

| Option | Meaning |
|---|---|
| `PROVIDER` | argument (required) |
| `--model` |  |
| `--name` |  |
| `--price-in` |  |
| `--price-out` |  |
| `--task` |  |
| `--vision` |  |
| `--reasoning` |  |

#### `litforge models catalogue`

| Option | Meaning |
|---|---|
| `--vision` |  |
| `--max-price` |  |
| `--search` |  |

#### `litforge models cloud`

| Option | Meaning |
|---|---|
| `MODE` | argument (required) |

#### `litforge models download`

| Option | Meaning |
|---|---|
| `REPO` | argument (required) |
| `FILE` | argument (required) |
| `--mmproj` |  |
| `--name` |  |
| `--task` |  |

#### `litforge models files`

| Option | Meaning |
|---|---|
| `REPO` | argument (required) |

#### `litforge models key`

Manage system keychain entries; environment variables take precedence.

#### `litforge models key remove`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |

#### `litforge models key set`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |

#### `litforge models key status`

#### `litforge models list`

| Option | Meaning |
|---|---|
| `--json` |  |

#### `litforge models pricing`

#### `litforge models pull-ollama`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |

#### `litforge models remove`

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--delete-files` |  |

#### `litforge models search`

| Option | Meaning |
|---|---|
| `TEXT` | argument (required) |
| `--show-all` |  |

#### `litforge models setup`

| Option | Meaning |
|---|---|
| `--tier` |  |
| `--cheap` |  |
| `--strong` |  |
| `--yes` |  |
| `--json` |  |

#### `litforge models start`

#### `litforge models status`

| Option | Meaning |
|---|---|
| `--json` |  |

#### `litforge models stop`

#### `litforge models test`

| Option | Meaning |
|---|---|
| `PROFILE` | argument (required) |
| `--vision` |  |
| `--approve` |  |

### `litforge calls`

Show recent model calls, usage, costs and status.

| Option | Meaning |
|---|---|
| `--run` |  |
| `--limit` |  |
| `--json` |  |

## App and maintenance

### `litforge ui`

Open the web interface in your browser (runs locally on Linux, macOS and Windows).

| Option | Meaning |
|---|---|
| `--port` | Port (0 = pick a free one) |
| `--host` | Interface to bind (keep 127.0.0.1) |
| `--no-browser` | Don't open a browser window |

### `litforge shortcut`

Add a LitForge icon to your apps that opens the web interface with one click.

### `litforge update`

Check for updates and print the terminal command; never install or pull.

| Option | Meaning |
|---|---|
| `--check` |  |

## Other

### `litforge desktop`

Open LitForge in its own window instead of a browser tab.

| Option | Meaning |
|---|---|
| `--workspace` | Folder whose data/ holds the library (remembered) |
| `--gui` | Window engine: gtk, qt, edgechromium or cocoa |

### `litforge desktop-setup`

Choose and prepare the window engine for `litforge desktop` (the installer runs this; no sudo needed).

| Option | Meaning |
|---|---|
| `--engine` | Linux: auto, gtk (the system's WebKitGTK) or qt |

### `litforge review`

Review flagged sentences and cells.

| Option | Meaning |
|---|---|
| `--project`, `-p` |  |
| `--json` |  |

#### `litforge review accept`

| Option | Meaning |
|---|---|
| `TARGET_ID` | argument (required) |
| `--reason` |  |

#### `litforge review edit`

| Option | Meaning |
|---|---|
| `TARGET_ID` | argument (required) |
| `VALUE` | argument (required) |
| `--reason` |  |

#### `litforge review reject`

| Option | Meaning |
|---|---|
| `TARGET_ID` | argument (required) |
| `--reason` |  |

### `litforge workflow`

Run built-in typed workflow templates.

#### `litforge workflow run`

Load a YAML workflow template into the same preview and execute path.

| Option | Meaning |
|---|---|
| `NAME` | argument (required) |
| `--param` | Workflow input key=value |
| `--project`, `-p` |  |
| `--yes`, `-y` |  |
| `--approve-cloud` | Separately approve cloud model routes |
| `--json` | Print the preview and run as JSON |

### `litforge write`

Writing Studio: LaTeX documents in a project.

#### `litforge write compile`

Compile a document to PDF.

| Option | Meaning |
|---|---|
| `DOCUMENT` | argument (required) |
| `--engine` | tectonic or xelatex |

#### `litforge write list`

List the project's documents.

| Option | Meaning |
|---|---|
| `--project`, `-p` | Project (default: the active project) |

#### `litforge write new`

Create a document from a template.

| Option | Meaning |
|---|---|
| `TITLE` | argument (required) |
| `--project`, `-p` | Project (default: the active project) |
| `--template` | article or persian |

