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

Implemented: import, parsing, search and Markdown export. In progress: paper discovery
(OpenAlex, Crossref, arXiv). Planned: screening with a local language model, deep analysis.

Known limitations of the fast parser:

- Tables drawn without vertical rules get a caption and crop but no cell grid (their text is
  still searchable).
- Equation text is approximate; the saved image crop is the reliable version.
- Some figures cannot be paired with their caption and get no crop.

Design documents (in Persian and English) are in [docs/](docs/).

## License note

PyMuPDF is AGPL-licensed. Review this before distributing LitForge or derived work.
