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
abstract are flagged). The decision and a 0–1 score are computed by fixed rules, not by the model;
a paper without an abstract is never excluded. Your ✓/✗ marks are separate and always decide.
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
