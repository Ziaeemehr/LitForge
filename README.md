# LitForge

A local-first tool for building a searchable library of scientific papers.
Drop in PDFs and get structured text with page references, figure and table crops,
full-text search and Markdown notes that open in Obsidian. Everything stays on your machine.

## Install

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:Ziaeemehr/LitForge.git
cd LitForge
uv sync
```

## Quick start

```bash
uv run litforge new my-review --topic "Attention mechanisms in deep learning"
uv run litforge add ~/papers
uv run litforge search "self-attention"
uv run litforge export
```

- `new` creates a project and makes it active, so later commands don't need `--project`.
- `add` imports, parses and indexes PDFs in one step. Run it again any time; nothing is processed twice.
- `search` returns the paper, section, page and a quoted snippet for every hit.
- `export` writes Markdown notes to `data/projects/my-review/exports/markdown/`.

## Web interface

```bash
uv run litforge ui        # opens http://127.0.0.1:8765 in your browser
```

One page to give a DOI, PDF or abstract (or search queries, or an Elicit/Zotero export), choose a
project and an output folder, and browse the results: summary numbers, publication years, how
papers were found, top venues, filters, and a quick view with the abstract, score breakdown and
full text (formulas rendered with KaTeX). Mark papers ✓/✗ and run "More like my ✓ papers" to
iterate. It runs locally with the Python standard library and works the same on macOS and Linux;
on a remote GPU machine use `ssh -L 8765:127.0.0.1:8765 host` and open the same address.

### Relevance, importance and review priority

Every result gets two scores, both between 0 and 1:

- **Relevance** to your seed or query: meaning (embeddings), shared references, citation links,
  agreement between search channels.
- **Importance**, independent of your query: field- and year-normalized citation percentile
  (OpenAlex), how many papers in the same result set cite it (core literature), citations in the
  last two years, and the venue's 2-year mean citedness. Retracted papers get 0 and a warning.

**Review priority** = 0.65 × relevance + 0.35 × importance, the default sort order, so a large
result set can be read from the top.

## Find papers

```bash
uv run litforge discover "self-attention"            # search OpenAlex, Crossref and arXiv
uv run litforge discover --seed 10.48550/arXiv.1706.03762   # follow references and citing papers
uv run litforge import refs.bib                      # add a .bib, .ris or .csv reference list
uv run litforge enrich                               # fill in missing DOI, year, venue, abstract
uv run litforge papers --no-pdf                      # ranked list of papers still missing a PDF
```

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
└── projects/<name>/      project settings and exports
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

Implemented: import, parsing, search, Markdown export and paper discovery (OpenAlex,
Crossref, arXiv, citation chaining, reference-list import). Planned: screening with a local
language model, deep analysis.

Known limitations of the fast parser:

- Tables drawn without vertical rules get a caption and crop but no cell grid (their text is
  still searchable).
- Equation text is approximate; the saved image crop is the reliable version.
- Some figures cannot be paired with their caption and get no crop.

Design documents (in Persian and English) are in [docs/](docs/).

## License note

PyMuPDF is AGPL-licensed. Review this before distributing LitForge or derived work.
