# MinerU as the accurate parser

## Why

A side-by-side run on 8 real pages (formulas, tables, the "2^10 to 2^17" superscript case) gave:

| | PyMuPDF (fast path) | Docling 2.x | MinerU 4.0 |
|---|---|---|---|
| Time per page (Apple M5) | ~0.5 s | ~95 s (formula enrichment) | ~18 s GPU, ~46 s CPU |
| Superscripts | lost (`217`) | split (`2 17`) | `2^{17}` |
| Display equations | plain text | several wrong symbols | accurate, keeps `\tag{n}` |
| Tables | not detected | rows merged into one cell | cell-accurate (1 of 6 garbled) |

Docling's merged table rows are unsafe for evidence (numbers lose their method). MinerU replaces Docling as
the structural fallback; PyMuPDF stays the default because it is ~40x faster.

## Install and isolation

- MinerU pulls torch and ~2 GB of models, so it never goes into LitForge's environment. LitForge runs it as
  an external command, like `llama-server`.
- `litforge parse install-mineru` (and a button on the settings page) creates `<data>/tools/mineru` with `uv`
  (fallback `python -m venv` + pip), installs a pinned `mineru[core]`, after showing the download size and
  asking for confirmation. Detection order: `parsing.mineru.command` in config, `<data>/tools/mineru/bin/mineru-kit`,
  `mineru-kit` on PATH.
- Models live in `<data>/models/mineru` (`MINERU_HOME`), next to the other managed models.
- Installation has a total 45-minute deadline (`parsing.mineru.install_timeout_s: 2700`). Failed,
  timed-out or interrupted installs remove only the tool directory created by that attempt; an existing
  installation or configured command is never removed.

## Privacy

- LitForge only ever calls `mineru-kit parse <pdf> -o <file> -f middle_json`; it never passes `--remote`,
  `--remote-url` or `--api-key`, and removes `MINERU_API_KEY` / `MINERU_VL_API_KEY` from the child environment.
  `CI=1` is set so the CLI never prompts for telemetry. A test asserts the exact argv and environment.

## Memory

- MinerU uses the GPU and fails with a Metal out-of-memory error when a large local LLM is loaded (observed
  with Gemma 4 26B on 24 GB). Before a MinerU run LitForge stops its *own* managed llama-server if it is idle
  (it autoloads again on the next call); it never touches servers it did not start.
- If MinerU still fails with an out-of-memory error, LitForge retries once with `MINERU_DEVICE_MODE=cpu` and
  records that in the parse run's reasons.
- Idle means every reported llama-server slot is inactive; busy or unavailable slot reporting leaves
  the server running. MinerU starts in a separate process group/session. A timeout terminates that
  group, allows a short grace period, then kills remaining children. Standard output is discarded;
  diagnostics are spooled to a temporary file and only the last 64 KiB of stderr enters memory.

## Mapping middle_json to the canonical document

`pages[].blocks[]` with `type`, `bbox` (0..1, top-left), `content`; page sizes come from
`extensions.docvortex_layout.pages[].width_pt/height_pt`.

- `paragraph_title` -> new `Section` (level from `level`; shared abstract/references/appendix detection
  with Docling). Appendix/supplementary headings may have a leading letter or numeric label.
- `text` -> `Paragraph(kind="text")`; spans are concatenated, `equation_inline` becomes `$...$`.
- `equation` -> `Paragraph(kind="equation")` with `$$...$$`.
- `table` -> `Asset(kind="table", data={"html", "markdown"})` plus a caption paragraph whose text is the caption
  and a plain-text rendering of the table (one row per line, cells separated by ` | `), so search, quotes and
  the matrix see each value next to its row and column. Expand rowspan/colspan into a rectangular grid,
  repeating spanned cells (including group headers). Keep the caption and table on separate lines through
  chunking and indexing.
- `chart`/`image` -> `Asset(kind="figure")` with caption; captions are paragraphs. A following text block
  starting with `Figure N` or `Fig. N` attaches to the immediately preceding figure on the same page.
- `header`, `footer`, `page_number` -> `furniture`, never body text.
- bbox -> PDF points: `(x0*w, y0*h, x1*w, y1*h)`.
- LaTeX is tidied: spaces between digits inside math are removed (`{ 1 0 }` -> `{10}`); LaTeX ignores those
  spaces, and it lets exact quotes like `2^{10}` match.

## Routing and use

- `parsing.complex_fallback`: `auto` (default; MinerU if installed, else Docling), `mineru`, `docling`, `none`.
  The `needs_docling` route becomes "needs structural parser".
- `litforge parse --parser mineru [--paper ID]` forces MinerU for chosen papers; the paper drawer gets
  "Re-read accurately (MinerU)" with an estimate shown as "about N min" and runs as a background job.
  Estimate seconds per page from the mean of successful MinerU runs' `elapsed_ms / page_count` in this
  library (including historical runs), falling back to 20 seconds per page.
- A new parse run becomes current only when it succeeds; then the paper is re-indexed. Old runs and their
  evidence spans stay (the ledger is immutable).

## Tests

- Fixture: a small hand-written middle_json (2 pages: heading, text with inline math and a superscript,
  display equation with `\tag`, a table with caption, a chart, header and page number).
- Mapping, bbox conversion, furniture, LaTeX tidying, section kinds.
- Command runner: argv/env (no remote flags, keys stripped, `MINERU_HOME`, `CI`), timeout, OOM -> CPU retry,
  failure recorded as a failed run, never an empty success.
- Routing with `auto` and both tools missing/present; `--parser mineru`; exact quote against `2^{10}`.
