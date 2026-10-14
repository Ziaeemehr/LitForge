# LitForge

Local-first system for discovering, screening and analysing scientific papers.
Design documents live in [docs/](docs/); this repository currently implements
**step 1: core library and PDF pipeline** (ingest → parse → index → search → export).

## Setup

The virtual environment lives outside the repo, under `~/envs/litforge`:

```bash
export UV_PROJECT_ENVIRONMENT=$HOME/envs/litforge   # add to your shell profile
uv sync                                             # base install
uv sync --extra docling                             # optional: Docling fallback parser (heavy)
```

Data (PDFs, parsed output, SQLite, logs) goes to `./data` by default. Override with
`LITFORGE_HOME=/path` or `--data-dir /path`.

## Usage

```bash
uv run litforge doctor
uv run litforge project create neuro-sbi --topic "Neural mass model inference from EEG/MEG"
uv run litforge ingest ./papers --project neuro-sbi
uv run litforge parse --project neuro-sbi
uv run litforge index --project neuro-sbi
uv run litforge search --project neuro-sbi "neural mass model"
uv run litforge status --project neuro-sbi
uv run litforge export --project neuro-sbi --format markdown
```

`ingest` and `parse` are idempotent and resumable; they accept `--dry-run`, `--force`
and `--json`.

### What happens to each PDF

1. **Ingest:** SHA256 is computed; the original is copied to `data/library/pdfs/<sha256>.pdf`
   and never deleted. Re-importing the same bytes only adds a filename alias and project
   membership. Corrupt or encrypted files are stored and marked `quarantined`.
2. **Parse:** PyMuPDF extracts text blocks with page numbers and bounding boxes, restores
   two-column reading order, detects sections, captions, figures and tables. Quality metrics
   feed a router that records one of `pymupdf`, `needs_docling` or `needs_ocr`. Output is
   cached under `data/library/parsed/<sha256>/<parser>-<version>-<confighash>/`
   (`canonical.json`, `paper.md`, `assets/`).
3. **Index:** section-aware chunks (~600 tokens, 80 overlap) go into SQLite FTS5 (BM25).
   References are not indexed by default.
4. **Search:** every hit returns paper, section, page range and a quoted snippet.

## Development

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Benchmark scripts:

```bash
uv run python scripts/fetch_benchmark.py          # downloads open-access arXiv PDFs to data/benchmark
uv run python scripts/benchmark_parsers.py        # writes data/benchmark/parser_benchmark.csv
```

## Baseline results (Apple M5, CPU, 10 arXiv papers)

From `scripts/benchmark_parsers.py` on the default `fetch_benchmark.py` set:

- PyMuPDF fast path: 18–80 ms/page; ~290 ms/page on a table-heavy paper (ResNet), dominated by
  `find_tables`. Whole set (10 papers, 155 pages) ingests + parses in ~10 s.
- Section trees (numbered, small-caps, letter-numbered appendices before/after References) are
  correct on all 10 papers after the fixes covered by `tests/unit/test_layout.py`.
- Search: all checked hits cite a page that actually contains the query term.
- Titles extracted for 10/10; DOIs are absent from arXiv PDFs (expected; resolve in the discovery step).

Known limitations to address next:

- Tables drawn only with horizontal rules (booktabs) get a caption-only asset, no cell grid;
  their text is still indexed. Docling (`needs_docling` route) is the intended fallback.
- Some figures lose their crop region when the caption cannot be paired with graphics
  (`figures_without_region` in the benchmark CSV).
- Characters in math fonts (e.g. ε in a title) may be dropped.
- `pymupdf_layout` is a candidate to benchmark against the current heuristics (check its license).

## GPU work

Everything in step 1 runs on CPU (developed on Apple Silicon). The following are deferred to
the RTX 5070 machine: LLM VRAM/throughput benchmarks, Docling GPU timing and bulk OCR.

## License note

PyMuPDF is AGPL-licensed. Fine for personal/research use; review before distributing.
