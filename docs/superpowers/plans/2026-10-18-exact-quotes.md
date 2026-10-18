# Exact quotes for Ask answers

## Context
Ask answers already cite evidence spans (paper, section, page, chunk text) per sentence, across up to
8 chunks from several papers. But a citation points at a whole chunk, so the reader cannot see *which
sentence* of the paper answers the question. Goal: for every cited span in every answer sentence, the
model also returns the verbatim supporting sentence(s); the system verifies each quote really occurs in
the transmitted evidence text and shows it highlighted. Retrieval scope (k=8, max 3 per paper) stays as is.

## Changes

1. **Quote locator** — `src/litforge/analysis/validation.py`
   - `locate_quote(quote: str, text: str) -> tuple[int, int] | None`: normalise both sides (casefold,
     collapse whitespace, drop soft hyphens and line-break hyphenation `-\n`, unify unicode dashes/quotes)
     while keeping an index map back to the original text; return original char offsets or `None`.
   - Match only against the packet item's text (what was actually sent, like `missing_numbers` does),
     so a quote from a part trimmed by `fit_evidence` is rejected.

2. **Draft and stored model** — `src/litforge/analysis/ask.py`
   - New `Quote(BaseModel)`: `evidence_id`, `text` (max ~400 chars), and after validation `start`, `end`.
   - `SentenceDraft.quotes: list[Quote] = []` (cap 3 per sentence, same truncate pattern as `cap_sentences`).
   - In `validate_sentences`: keep a quote only if its `evidence_id` is in the sentence's valid `ids`
     and `locate_quote` finds it in that packet item; otherwise drop it and add flag
     `quote_not_in_evidence`. A `reported_fact` with cited ids but no surviving quote gets `unquoted_fact`
     (so it shows under `needs_review`, consistent with existing flag handling).
   - Persist quotes: insert `quotes_json` alongside `evidence_ids_json`; read it back in `get_sentences`.

3. **Migration** — `src/litforge/storage/migrations/0019_answer_quotes.sql`:
   `ALTER TABLE answer_sentences ADD COLUMN quotes_json TEXT NOT NULL DEFAULT '[]';`
   Old answers load with empty quotes.

4. **Prompt** — `src/litforge/analysis/prompts.py`
   - `ask_messages`: ask for `quotes` per sentence: for each cited evidence ID, copy the shortest
     verbatim passage (one sentence) from that evidence that supports the claim; never paraphrase.
   - Bump `ASK_PROMPT_VERSION` to `"ask-v3"` (eval runs pick it up via `evaluation.py`).

5. **API** — `src/litforge/ui/api.py`: sentences are serialised via `model_dump`, so quotes flow through;
   check the review/drawer endpoints around lines 462–527 still build sentences correctly.

6. **UI** — `src/litforge/ui/static/app.js` (+ small style in `app.css`)
   - `lensSentence` (~line 2160): under each sentence, show each verified quote as a short `blockquote`
     with chip "`title · p.N`"; clicking opens the drawer for that span.
   - `lensEvidence` (~line 2097): accept optional highlight ranges; when opened from a quote, render the
     span text with `<mark>` over `[start, end)` (build with text nodes, no innerHTML) and expand the
     excerpt so the mark is visible.
   - Add labels for new flags next to `number_not_in_evidence` (~line 2036): `quote_not_in_evidence` →
     "Quote not in evidence", `unquoted_fact` → "No exact quote".

7. **Docs** — one short paragraph in `docs/guide.md` Ask section about exact quotes and the two flags.

## Tests
- `tests/unit/test_analysis_ask.py`: parametrised `validate_sentences` cases — exact quote kept with
  correct offsets; quote with different whitespace/line-break hyphen still found; paraphrase dropped +
  `quote_not_in_evidence`; quote citing an id not in the sentence dropped; fact without quote →
  `unquoted_fact`; quotes from two different papers in one sentence both kept; round-trip through
  `ask` → `get_answer` preserves quotes; prompt version is `ask-v3`.
- Unit tests for `locate_quote` edge cases (case, unicode dash, not found).
- `tests/ui/lens.test.cjs`: sentence renders quote; drawer marks the right range.
- Update the fake gateway in `tests/lens_fakes` if it builds `AnswerDraft` replies, so it returns a quote.

## Verification
- `uv run pytest tests/unit/test_analysis_ask.py tests/integration/test_ui_lens.py -q`, then full
  `uv run pytest -q` and `uv run ruff check`.
- `node --test tests/ui/lens.test.cjs`.
- Manual: run the app, ask a question over a few parsed papers, confirm each sentence shows quotes from
  the right papers, the drawer highlights them, and "Open PDF at page N" lands on the page containing it.
