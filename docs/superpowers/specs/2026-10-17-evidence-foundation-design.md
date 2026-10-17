# Evidence Foundation (roadmap phase 0, part 2) — design

**Date:** 2026-10-17
**Roadmap:** docs/local_ai_research_roadmap_fa.md, phase 0. Part 1 (provider contracts, router, budget, call
audit table) is done; this part wires it together and adds the evidence backbone.

## Goal

Answer a research question with retrieved, exactly cited evidence and no free synthesis, while every model call
from now on goes through one guarded dispatcher, and retrieval quality can be measured on a labelled benchmark.

## Scope

In scope:

1. **ModelGateway** — one dispatcher combining ModelRouter, privacy config, BudgetGuard, the provider and the
   model_calls audit. Every later feature calls models only through it.
2. **Evidence Ledger** — immutable `evidence_spans` table and module; snapshots of retrieved chunk text with
   full provenance.
3. **Analysis runs** — `analysis_runs` table recording task, project, config, prompt version, input hash,
   status and result summary; model calls link to their run.
4. **Question retrieval** — natural-language questions retrieve with OR semantics over content words (the
   current `search` ANDs every term, which fails for questions).
5. **Evaluation harness** — retrieval benchmark file format, Recall@k / hit-rate@k / MRR, CLI command.
6. **`litforge ask --evidence-only`** — the phase-0 deliverable: evidence cards (quote, paper, section, page,
   evidence ID), stored as an analysis run.

Out of scope: model-written answers (phase 1), embeddings in retrieval (added only when the benchmark proves a
gain — the harness must make that comparison possible but this part ships lexical retrieval only), UI changes.

## Components

### Package `litforge.analysis`

New package. Files and responsibilities:

- `gateway.py` — `ModelGateway`, `CallPreview`, `GatewayError`.
- `ledger.py` — `EvidenceSpan`, `record_spans`, `get_spans`, `to_packet`.
- `runs.py` — `start_run`, `finish_run`, `AnalysisRun`.
- `retrieve.py` — `retrieve_for_question`.
- `evaluation.py` — `RetrievalBenchmark`, `evaluate_retrieval`, `RetrievalReport`.

### ModelGateway

```python
class ModelGateway:
    def __init__(self, conn, config: Config, providers: Mapping[str, ProviderLike] | None = None,
                 provider_factory: Callable[[str, ModelProfileConfig], ProviderLike] | None = None) -> None
    def preview(self, task, evidence: EvidencePacket, messages, profile: str | None = None,
                max_output_tokens: int = 1024) -> CallPreview
    def call(self, task, evidence, messages, schema, profile: str | None = None, approved: bool = False,
             run_id: str | None = None, max_output_tokens: int = 1024, prompt_version: str = "") -> ModelReply
```

- Profiles: `"local"` is `config.models.local`; others come from `config.models.profiles`. Build
  `ModelRouter` profiles from config. `ModelProfileConfig` gains optional `pricing: Pricing | None` (input and
  output USD per million tokens). Local profiles cost 0 and need no pricing.
- Default provider factory: `ollama` → `OllamaClient` (url from `LITFORGE_OLLAMA_URL` or
  `config.screening.ollama_url`), `openai_compatible` → `OpenAICompatibleClient(base_url, model, api_key_env)`.
  Unknown provider → `GatewayError`. Providers are created lazily and cached per profile name.
- Order of checks in `call`, all before any transport access:
  1. route via `ModelRouter.route(ModelRequest(task, evidence, preferred_profile, allow_cloud), policy, approved)`
     where `allow_cloud = config.privacy.allow_cloud`;
  2. if the chosen profile is remote and `config.privacy.allow_cloud` is false → `RoutingError`;
  3. if remote and the packet is sensitive and `require_confirmation_for_sensitive_content` and not approved →
     `CloudApprovalRequired`;
  4. remote: `BudgetGuard.check(pricing, estimated_input_tokens, max_output_tokens, spent_run, spent_month)`
     where `spent_run` = sum of `cost_usd` of model_calls with this `run_id` (0 without run),
     `spent_month` = sum since the first day of the current UTC month.
- Token estimate: `ceil(total characters of all message contents / 4)`.
- After dispatch: normalise a plain dict reply (Ollama, FakeLlm) into `ModelReply(provider=profile.provider,
  model=profile.model, usage=ModelUsage())`. Compute actual cost from usage (0 for local). Record one
  `model_calls` row for every dispatched call — success (`status="ok"`), provider error (`status="error"`,
  error text without secrets, then re-raise) or actual cost over budget (`status="over_budget"`, recorded, then
  raise `BudgetExceeded`). Calls rejected before dispatch are not recorded as model calls.
- `input_hash` = sha256 of canonical JSON of `{"messages", "schema", "evidence_hash", "model", "provider"}`.
- `CallPreview` fields: `profile`, `provider`, `model`, `remote`, `evidence_ids`, `estimated_input_tokens`,
  `max_output_tokens`, `max_cost_usd` (None when remote pricing is missing), `requires_approval` (bool),
  `blocked_reason` (str | None, e.g. "cloud disabled", "local_only policy", "missing pricing",
  "over budget"). `preview` never dispatches and never raises for policy reasons; it reports them.

### Migration 0008_evidence.sql

```sql
CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    task TEXT NOT NULL, input_hash TEXT NOT NULL, prompt_version TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,      -- running|ok|error|cancelled
    result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
    started_at TEXT NOT NULL, finished_at TEXT);
CREATE INDEX idx_analysis_runs_project ON analysis_runs(project_id, task);

ALTER TABLE model_calls ADD COLUMN run_id TEXT;
ALTER TABLE model_calls ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_model_calls_run ON model_calls(run_id);

CREATE TABLE evidence_spans (
    id TEXT PRIMARY KEY,                 -- "ev_" + first 16 hex of sha256(parse_run_id|chunk_id|content_hash)
    paper_id TEXT NOT NULL, parse_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    parser TEXT NOT NULL, parser_version TEXT NOT NULL,
    kind TEXT NOT NULL, section TEXT, page INTEGER NOT NULL, page_start INTEGER NOT NULL, page_end INTEGER NOT NULL,
    bbox_json TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX idx_evidence_spans_paper ON evidence_spans(paper_id);
CREATE TRIGGER evidence_spans_no_update BEFORE UPDATE ON evidence_spans
BEGIN SELECT RAISE(ABORT, 'evidence spans are immutable'); END;
CREATE TRIGGER evidence_spans_no_delete BEFORE DELETE ON evidence_spans
BEGIN SELECT RAISE(ABORT, 'evidence spans are immutable'); END;

CREATE TABLE run_evidence (
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence_spans(id),
    rank INTEGER NOT NULL, score REAL, PRIMARY KEY (run_id, evidence_id));
```

No foreign key from evidence spans to chunks: chunks are replaced on re-index, spans must survive.
`ModelCallRecord` gains `run_id: str | None` and `prompt_version: str`.

### Evidence Ledger

- `record_spans(conn, hits: Sequence[SearchHit]) -> list[EvidenceSpan]` reads the chunk row and its parse run
  (parser, parser_version), snapshots the full chunk `text`, uses the hit page, and `INSERT OR IGNORE`s; returns
  spans in hit order. Same chunk text twice yields the same ID.
- `get_spans(conn, ids) -> list[EvidenceSpan]` in requested order; unknown ID → `KeyError`.
- `to_packet(spans, sensitive=False, max_chars_per_item=2000) -> EvidencePacket` — text truncated on a word
  boundary with "…", never including anything that is not a span.
- `EvidenceSpan.citation` property → `"<title or paper_id>, p.<page>"` needs the title, so `EvidenceSpan` carries
  `title: str | None` filled from `papers` on read (not stored).

### Runs

`start_run(conn, task, project_id, input_payload: dict, config: dict, prompt_version="") -> str` stores
`input_hash` (sha256 of canonical JSON of input_payload). `finish_run(conn, run_id, status, result: dict,
error=None)`. `link_evidence(conn, run_id, spans, scores)`.

### Question retrieval

- `fts.build_any_match(query) -> tuple[str, list[str]]`: lowercase word tokens, drop an English stopword set
  (kept in `litforge/indexing/stopwords.py`, ~150 common words incl. question words) and tokens shorter than 2
  chars, dedupe, keep quoted phrases as phrases, join with ` OR `. Returns "" when nothing is left.
- `fts.search(..., any_terms: bool = False)`; `any_terms=True` uses `build_any_match`. Default unchanged.
- `retrieve_for_question(conn, question, project_id=None, k=8, max_per_paper=3) -> list[SearchHit]` — calls
  search with `any_terms=True`; if a phrase-free question returns nothing, returns [].

### Evaluation harness

Benchmark file, YAML or JSON:

```yaml
name: my-benchmark
questions:
  - id: q1
    question: How are neural mass parameters inferred?
    relevant:                       # each target is one piece of evidence that should be retrieved
      - paper: 10.1234/synthetic.5678   # paper id, DOI or arXiv id
        page: 2                     # optional: hit chunk must cover this page
        contains: Jansen Rit        # optional: case-insensitive substring of the chunk text
```

- `load_benchmark(path) -> RetrievalBenchmark` (pydantic; question ids unique; at least one target each).
- A hit satisfies a target when the paper matches (`papers.id`, `papers.doi` case-insensitive, or
  `papers.arxiv_id`), the page condition holds (`page_start <= page <= page_end`) and the substring is present.
- `evaluate_retrieval(conn, bench, k=10, project_id=None, retriever=retrieve_for_question) -> RetrievalReport`
  with per-question `recall`, `hit` (any target found), `reciprocal_rank`, `missed` targets, and aggregates
  `recall_at_k`, `hit_rate_at_k`, `mrr`. The retriever is injectable so an embedding retriever can be compared
  later without changing the harness.
- The report is stored as an analysis run with task `eval_retrieval`.
- `configs/benchmark.example.yaml` documents the format. A real 30-question benchmark needs the user's papers;
  docs explain how to write one. Tests use the synthetic PDFs from `tests/conftest.py`.

### CLI

- `litforge ask QUESTION --evidence-only [-p PROJECT] [-k 8] [--json]` prints numbered evidence cards:
  `[ev_xxx] Title — Section, p.N` and a quote (the search snippet). JSON emits run_id and spans. Without
  `--evidence-only`, this part prints "model answers arrive in phase 1; use --evidence-only" and exits 2.
  Records an `ask_evidence` analysis run with linked evidence.
- `litforge eval retrieval BENCHMARK [-p PROJECT] [-k 10] [--json]` prints aggregates and the missed targets.
- `litforge calls [--run RUN_ID] [--limit 20] [--json]` lists recent model calls (provider, model, task, tokens,
  cost, latency, status) — the visible audit log.

## Error handling

Typed errors only: `RoutingError`, `CloudApprovalRequired`, `BudgetExceeded`, `GatewayError`, `LlmError`,
`LlmUnavailable`. CLI turns them into one-line messages with exit code 1. No secret or Authorization header
appears in errors, logs or the database.

## Testing

Unit tests (no network): gateway routing order, cloud-disabled rejection before transport, sensitive packet
approval, budget per-run and per-month from recorded calls, missing pricing, audit rows for ok/error/over-budget,
reply normalisation, preview reporting; ledger immutability triggers, idempotent IDs, packet truncation;
`build_any_match` (stopwords, phrases, empty); benchmark loading/validation and metric arithmetic on a fake
retriever. Integration: synthetic PDF → parse → index → `ask --evidence-only` → spans and run stored; `eval
retrieval` on a two-question benchmark. Existing tests stay green; Ruff and strict mypy pass.
