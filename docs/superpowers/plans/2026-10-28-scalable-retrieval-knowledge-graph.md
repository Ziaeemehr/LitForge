# Scalable Retrieval, Paper Cards, Knowledge Graph and MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This document is a proposal for review, not authorization to start implementation.

**Goal:** Make question answering and writing assistance hold up on a library of about 500 papers. Retrieval should find the right evidence for both narrow and broad questions. The evidence budget should follow the model in use, so a cloud model with a large context can get more evidence. A knowledge graph should connect papers through shared methods, datasets and claims. A read-only MCP server should let external agents use the library without reading the raw text files.

**Architecture:** Retrieval becomes a pipeline of channels fused by Reciprocal Rank Fusion: BM25 (existing `chunks_fts`), dense chunk vectors (existing `fastembed` embedder), and later paper cards and graph expansion. An optional cross-encoder reranks the fused candidates. A question-scope step chooses between a *focused* path (chunks) and a *broad* path (paper cards first, then the best chunks per selected paper). Everything derived from a paper (vectors, cards, entities) is built once per parse run, keyed by content hash and prompt version, and stored in SQLite, so nothing is re-read on later days. The evidence-first rules stay: every answer sentence still cites verified spans, and cards and entities are only routing aids, never evidence by themselves.

**Tech Stack:** Python 3.12, stdlib `sqlite3` with numbered migrations (next is `0033`), pydantic, typer, `fastembed` 0.9.0 (already in the `embed` extra: `TextEmbedding` for vectors, `TextCrossEncoder` for reranking), numpy (already a `fastembed` dependency), the official `mcp` Python SDK as a new optional extra `mcp`, pytest.

**Spec:** This plan; it came out of the 2026-10-28 chat about token use and retrieval at 500 papers. Related: [docs/literature_analysis_architecture_plan.md](../../literature_analysis_architecture_plan.md), the sbi10 benchmark in [benchmarks/sbi10/](../../../benchmarks/sbi10/) and its [baseline](../../../benchmarks/sbi10/baseline-2026-10-23.md).

## Findings that motivate the plan (current code, 2026-10-28)

- `retrieve_for_question` ([src/litforge/analysis/retrieve.py](../../../src/litforge/analysis/retrieve.py)) is BM25 only, with `k=8` and `max_per_paper=3` (the writing assistant uses `max_per_paper=2`). At most 8 papers can ever appear in one answer.
- The cloud model gets the same 8 chunks. `ModelGateway.fit_evidence` only *removes* evidence when it does not fit `context_tokens`; it never adds evidence when there is room.
- sbi10 baseline: English Recall@10 is 67%. Claim clustering with TF-IDF never pairs claims across papers (max similarity 0.27 < 0.55), so the claim graph has 0 relations.
- sbi10 has 10 papers and no broad (many-paper) questions, so it cannot show the 500-paper problem yet.

## Scope and order

- **Milestone A — measure and widen (Tasks 1–3):** a scale benchmark, a model-aware evidence budget, hybrid retrieval. This alone answers most of the "is 8 chunks enough" concern.
- **Milestone B — broad questions (Tasks 4–6):** reranker, paper cards, the broad-question path with map-reduce answering.
- **Milestone C — knowledge graph (Tasks 7–8):** entities and edges from cards and claims, embedding-based claim clustering, graph expansion as a retrieval channel.
- **Milestone D — MCP (Task 9):** a read-only stdio MCP server over the same functions.
- **Ship rule:** every task is shippable alone and must not lower any sbi10 metric. Each milestone ends with `litforge eval all` on sbi10 and on the scale benchmark, compared with the previous numbers.
- **Out of scope:** an external vector database, a graph database, Persian-specific retrieval changes (questions are assumed English for now), and UI graph visualisation (a later plan).

## Global Constraints

- Questions are English for this plan; the existing Persian→English `search_query` stays unchanged and in front of retrieval.
- No new required runtime dependency. Dense retrieval, the reranker and cards degrade to the current lexical path when `fastembed` or the model is missing, with a `doctor` notice.
- Everything derived from a paper is cached by `(parse_run_id, model or prompt version)` and rebuilt only when that key changes.
- Cloud calls that build cards or entities go through `ModelGateway` with their own task names and the existing approval and budget rules; the default profile for them is local.
- Answer sentences may cite only chunk or asset spans from the evidence ledger. Paper cards and graph edges are never cited directly.
- Default evidence budgets: local profiles `6000` tokens, remote profiles `24000` tokens, both configurable; never more than `context_tokens − output cap`.
- Vector search is brute force with numpy over float32 unit vectors (500 papers ≈ 30 000 chunks × 384 dims ≈ 46 MB); no ANN index unless a measurement shows it is needed.
- Every execution follows AGENTS.md: three rounds of run → inspect → diagnose → fix → rerun, recorded in this plan's validation section.

## Review Focus

1. **Library without embeddings** (`embedding_model: null` or `fastembed` not installed): every path must still answer with the lexical channel, not crash. Test in Task 3.
2. **Stale vectors after a re-parse:** vectors of an old parse run must never be returned. Test in Task 3.
3. **Broad question on a tiny library** (fewer papers than the broad-path paper limit): must not loop or duplicate evidence. Test in Task 6.
4. **Card fields that cite text not in the paper:** card fields whose quote does not verify are dropped, not kept. Test in Task 5.
5. **MCP client asking for a cloud-backed tool:** the MCP server must never trigger a remote model call. Test in Task 9.

---

### Task 1: Scale benchmark with distractors and broad questions

**Files:**
- Create: `benchmarks/scale500/papers.txt` (pinned arXiv ids: the sbi10 papers plus about 490 distractors from the same and nearby fields, chosen by a documented arXiv query and frozen)
- Create: `benchmarks/scale500/benchmark.yaml` (all sbi10 questions plus 12 broad questions)
- Create: `benchmarks/scale500/README.md` (how the list was built, how to download, expected run time)
- Modify: `src/litforge/analysis/evaluation.py` (broad-question metrics)
- Test: `tests/unit/test_evaluation_broad.py`

**Interfaces:**
- Produces: a `BroadQuestion` model in `evaluation.py` with `id: str`, `question: str`, `papers: list[str]` (gold arXiv ids, at least 5 per question); a `paper_recall(hits: list[SearchHit], gold: list[str]) -> float`; and a new field `evidence_tokens: int` on each per-question result so token use is reported next to recall.

- [ ] **Step 1: Write the failing tests** `test_paper_recall_counts_distinct_papers` (hits from papers a, a, b against gold a, b, c give `2/3`) and `test_benchmark_loads_broad_questions` (a YAML with a `broad:` list parses into `BroadQuestion` items).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_evaluation_broad.py -v` — expected FAIL (names undefined).
- [ ] **Step 3: Implement** `BroadQuestion`, `paper_recall` and the `broad:` section in the benchmark loader; `eval all` prints `Paper recall (broad)` and the mean evidence tokens per question.
- [ ] **Step 4: Write the 12 broad questions** (for example "Which papers use normalizing flows for posterior estimation?") with gold papers checked by Claude against the indexed text, the same way sbi10 labels were made.
- [ ] **Step 5: Run** the tests — expected PASS; then ingest the corpus in a scratch data dir and record the lexical baseline (chunk Recall@10, paper recall, evidence tokens) in `benchmarks/scale500/baseline-<date>.md`.
- [ ] **Step 6: Commit** `feat(eval): add scale500 benchmark with broad questions`.

### Task 2: Evidence budget that follows the model

**Files:**
- Modify: `src/litforge/config.py` (`ModelProfileConfig.evidence_tokens: int | None = None`)
- Modify: `src/litforge/analysis/gateway.py` (new `evidence_budget`)
- Modify: `src/litforge/analysis/ask.py`, `src/litforge/writing/chat.py`, `src/litforge/writing/assist.py`, `src/litforge/writing/cite.py` (derive `k` from the budget instead of the fixed `8`)
- Test: `tests/unit/test_evidence_budget.py`

**Interfaces:**
- Produces: `ModelGateway.evidence_budget(task: str, profile: str | None = None) -> int`, which returns `evidence_tokens` if set, else `24000` for remote profiles and `6000` for local ones, clipped to `context_tokens − output cap − 1500` (prompt overhead). Also `chunks_for_budget(budget: int, chunk_tokens: int) -> int` in `retrieve.py`, which returns `max(4, budget // chunk_tokens)` with `chunk_tokens` from `config.indexing.chunk_target_tokens`.
- `max_per_paper` becomes `max(2, k // 4)` for focused questions.

- [ ] **Step 1: Write the failing tests**: `test_remote_profile_gets_large_budget` (remote, no context limit → `24000`); `test_budget_clipped_by_context` (`context_tokens=8192`, output cap 2048 → `4644`); `test_explicit_evidence_tokens_wins`; `test_chunks_for_budget` (`24000, 600` → `40`; `1000, 600` → `4`).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** both functions and replace the fixed `k` in the four callers; the CLI `--k` option, when given, still overrides.
- [ ] **Step 4: Run** the unit tests and the full suite `uv run pytest -q` — expected PASS; the run record (`start_run` input) now stores the budget and `k`.
- [ ] **Step 5: Commit** `feat(ask): size evidence to the model's budget`.

### Task 3: Hybrid retrieval (BM25 + dense vectors, RRF)

**Files:**
- Create: `src/litforge/storage/migrations/0033_chunk_vectors.sql` (`chunk_vectors(chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE, model TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL)`)
- Create: `src/litforge/indexing/vectors.py`
- Modify: `src/litforge/indexing/fts.py` (`index_run` embeds new chunks when an embedder is available)
- Modify: `src/litforge/analysis/retrieve.py`
- Modify: `src/litforge/doctor.py` (notice when vectors are missing or built with another model)
- Modify: `src/litforge/cli.py` (`litforge index --vectors` backfills vectors for an existing library)
- Test: `tests/unit/test_vectors.py`, `tests/unit/test_hybrid_retrieve.py`

**Interfaces:**
- Consumes: `Embedder`, `load_embedder` from `src/litforge/discovery/textsim.py`; `config.similar.embedding_model` and `embedding_device`.
- Produces: `embed_chunks(conn, embedder: Embedder, chunk_ids: list[str]) -> int`; `dense_search(conn, embedder, query: str, project_id: str | None, limit: int, paper_ids: list[str] | None = None) -> list[SearchHit]` (only current parse runs, cosine similarity as `score`); `rrf_fuse(channels: list[list[SearchHit]], k: int = 60) -> list[SearchHit]`; and `retrieve_for_question(..., embedder: Embedder | None = None)` with an unchanged return type. Vectors are stored as float32 unit vectors; the query uses the `bge` query prefix `"Represent this sentence for searching relevant passages: "`.

- [ ] **Step 1: Write the failing tests**: `test_rrf_prefers_hits_in_both_channels`; `test_dense_search_ignores_old_parse_runs` (Review Focus 2); `test_retrieve_without_embedder_equals_lexical` (Review Focus 1: same hits as today); `test_index_run_embeds_new_chunks` (a fake embedder records its calls; a second `index_run` returns `cached` and makes no new embed call).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** the migration, `vectors.py` and the fusion. Each channel fetches `3 × k` candidates; diversity (`max_per_paper`) and the front-matter demotion are applied after fusion.
- [ ] **Step 4: Run** the unit tests and the full suite — expected PASS.
- [ ] **Step 5: Measure** with `litforge eval all` on sbi10 and scale500; the expected result is that chunk Recall@10 on sbi10 rises above 67%. Record the numbers in the validation section.
- [ ] **Step 6: Commit** `feat(retrieve): fuse BM25 and dense chunk vectors`.

### Task 4: Cross-encoder reranking

**Files:**
- Create: `src/litforge/indexing/rerank.py`
- Modify: `src/litforge/config.py` (`IndexingConfig.reranker_model: str | None = "Xenova/ms-marco-MiniLM-L-6-v2"`, `rerank_candidates: int = 50`)
- Modify: `src/litforge/analysis/retrieve.py`
- Test: `tests/unit/test_rerank.py`

**Interfaces:**
- Produces: a `Reranker` Protocol with `score(query: str, texts: Sequence[str]) -> list[float]`; `load_reranker(model: str | None, cache_dir: Path | None) -> Reranker | None` (wraps `fastembed.rerank.cross_encoder.TextCrossEncoder`; returns `None` if it is unavailable); and `retrieve_for_question(..., reranker: Reranker | None = None)`, which reranks the top `rerank_candidates` fused hits before diversity is applied.

- [ ] **Step 1: Write the failing tests**: `test_reranker_reorders_candidates` (a fake reranker reverses the order); `test_missing_reranker_keeps_fused_order`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement**; the chunk text is truncated to 512 tokens before scoring.
- [ ] **Step 4: Run** the tests, then `eval all` on both benchmarks; keep the reranker on by default only if Recall@10 improves and the added latency per question stays under 1.5 s on CPU. Record both numbers.
- [ ] **Step 5: Commit** `feat(retrieve): optional cross-encoder rerank`.

### Task 5: Paper cards (built once per paper)

**Files:**
- Create: `src/litforge/storage/migrations/0034_paper_cards.sql` (`paper_cards(parse_run_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, prompt_version TEXT NOT NULL, card_json TEXT NOT NULL, vector BLOB, created_at TEXT NOT NULL)`)
- Create: `src/litforge/analysis/cards.py`
- Modify: `src/litforge/config.py` (add `"paper_card"` to `TASK_NAMES`; default task profile local)
- Modify: `src/litforge/analysis/prompts.py` (`CARD_PROMPT_VERSION = "card-v1"`, `card_messages`)
- Modify: `src/litforge/cli.py` (`litforge cards build [--project] [--force]`, with a cost preview for remote profiles)
- Test: `tests/unit/test_cards.py`

**Interfaces:**
- Consumes: `retrieve_for_question` restricted with `paper_ids=[paper_id]` to collect the abstract, introduction, method and conclusion chunks (at most 6 000 tokens per paper); `ModelGateway.call`; the quote validation in `analysis/validation.py`.
- Produces: a `PaperCard` model with `paper_id`, `summary: str` (≤ 120 words), `research_question`, `methods: list[str]`, `datasets: list[str]`, `metrics: list[str]`, `findings: list[CardFinding]` (`text` + `evidence_id` + `quote`); `build_card(conn, gateway, paper_id: str, force: bool = False) -> PaperCard`; `get_card(conn, paper_id: str) -> PaperCard | None`; `card_search(conn, embedder, query: str, project_id: str | None, limit: int) -> list[tuple[str, float]]` (paper id, score; dense search over card vectors, BM25 over card text when there is no embedder).

- [ ] **Step 1: Write the failing tests**: `test_card_cached_per_parse_run` (a second build makes no gateway call); `test_card_rebuilt_after_reparse`; `test_unverified_finding_dropped` (Review Focus 4); `test_card_search_ranks_matching_method_first`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** with a fake gateway in the tests; a real local run with `qwen3:8b` on sbi10 checks that the cards read correctly (inspect 3 cards by hand and note them in the validation section).
- [ ] **Step 4: Run** the tests and the full suite — expected PASS.
- [ ] **Step 5: Commit** `feat(cards): cached per-paper cards with verified findings`.

### Task 6: Broad-question path with map-reduce answers

**Files:**
- Create: `src/litforge/analysis/scope.py`
- Modify: `src/litforge/analysis/retrieve.py`, `src/litforge/analysis/ask.py`
- Test: `tests/unit/test_scope.py`, `tests/unit/test_broad_ask.py`

**Interfaces:**
- Consumes: `card_search`, `evidence_budget`, `chunks_for_budget`.
- Produces:
  - `question_scope(question: str) -> Literal["focused", "broad"]`. It is deterministic: `broad` when the question matches patterns such as `which papers|what methods|compare|across|survey|all studies|list` or asks for counts; otherwise `focused`. The user can override it with `--scope`.
  - `retrieve_broad(conn, question, project_id, budget: int, embedder, reranker, max_papers: int = 40) -> list[SearchHit]`. It selects papers by `card_search` fused with paper-level BM25, then takes the 1–2 best chunks inside each selected paper.
  - `ask(..., scope: Literal["auto", "focused", "broad"] = "auto")`. When the broad evidence exceeds the budget, it answers in batches that fit the budget (map step: cited partial answers), then runs one merge call over the partial sentences and their spans (reduce step). Citations keep the original evidence ids, so validation is unchanged.

- [ ] **Step 1: Write the failing tests**: `test_scope_detects_broad_patterns` (5 broad and 5 focused examples); `test_broad_retrieval_covers_many_papers` (a fixture of 20 papers with the term in 12 of them → evidence from ≥ 10 papers); `test_broad_on_tiny_library` (Review Focus 3: 3 papers, no duplicates, terminates); `test_map_reduce_keeps_evidence_ids` (a fake gateway; every merged sentence cites an id from the map step).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement**; the map step runs at most `ceil(evidence / budget)` calls, capped at 6. With a remote profile the preview shows the total cost before approval.
- [ ] **Step 4: Run** the tests, then `eval all` on scale500; paper recall on broad questions is the main number. Record it.
- [ ] **Step 5: Commit** `feat(ask): broad questions via paper cards and map-reduce`.

### Task 7: Knowledge graph tables and builder

**Files:**
- Create: `src/litforge/storage/migrations/0035_knowledge_graph.sql`: `kg_entities(id, kind CHECK(kind IN ('method','dataset','metric','task','concept')), name, norm_name, UNIQUE(kind, norm_name))`, `kg_mentions(entity_id, paper_id, parse_run_id, span_id NULL)`, `kg_edges(src_kind, src_id, rel, dst_kind, dst_id, weight, evidence_json)`
- Create: `src/litforge/analysis/kg.py`
- Modify: `src/litforge/analysis/claim_graph.py` (`cluster_claims` uses embedding similarity when an embedder is available, keeping TF-IDF as the fallback)
- Modify: `src/litforge/cli.py` (`litforge kg build`, `litforge kg show <entity>`, `litforge kg neighbors <paper>`)
- Test: `tests/unit/test_kg.py`, `tests/unit/test_claim_graph_embed.py`

**Interfaces:**
- Consumes: `PaperCard.methods/datasets/metrics`, claims from `list_claims`, the embedder.
- Produces: `build_graph(conn, project_id: str, embedder: Embedder | None) -> GraphStats` (counts of entities, mentions and edges; incremental, so only papers whose card changed are touched); `normalize_entity(name: str) -> str` (NFKC, lowercase, strip punctuation, singularize a trailing `s`, plus a small alias table such as `nf → normalizing flow`); `entity_papers(conn, entity_id) -> list[str]`; `paper_neighbors(conn, paper_id, limit: int = 10) -> list[tuple[str, float]]` (shared entities weighted by IDF, plus claim relations). The edge kinds are `paper-uses-entity`, `paper-shares-entity-paper`, `claim-supports|contradicts-claim` (from `claim_graph`) and `paper-cites-paper` (from existing references, where they are resolved).

- [ ] **Step 1: Write the failing tests**: `test_normalize_merges_variants` (`"Normalizing Flows"`, `"normalizing flow"` → same key); `test_build_graph_incremental` (rebuilding after one card changes touches only that paper's mentions); `test_neighbors_rank_by_shared_rare_entity`; `test_embedding_clustering_pairs_cross_paper_claims` (two paraphrased claims from different papers fall in one cluster with a fake embedder).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the tests; then `litforge eval claims` on sbi10; the expected result is that cross-paper relations rise above 0 and claim recall rises above 36%. Record both.
- [ ] **Step 5: Commit** `feat(kg): entity graph from cards and claims`.

### Task 8: Graph expansion as a retrieval channel

**Files:**
- Modify: `src/litforge/analysis/retrieve.py`, `src/litforge/analysis/kg.py`
- Test: `tests/unit/test_kg_retrieve.py`

**Interfaces:**
- Produces: `link_question_entities(conn, question: str, project_id) -> list[str]` (entity ids whose `norm_name` appears in the normalized question, longest match first); `graph_channel(conn, question, project_id, limit) -> list[SearchHit]` (the best chunk, by BM25 inside the paper, of each paper that mentions a linked entity); this becomes the third channel in `rrf_fuse` and the second paper source in `retrieve_broad`.

- [ ] **Step 1: Write the failing tests**: `test_graph_channel_finds_paper_without_query_words` (the paper names the dataset only by its alias, and the alias table links it); `test_no_entities_means_empty_channel`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the tests and `eval all` on both benchmarks; keep the channel on by default only if neither benchmark gets worse.
- [ ] **Step 5: Commit** `feat(retrieve): knowledge-graph expansion channel`.

### Task 9: Read-only MCP server

**Files:**
- Create: `src/litforge/mcp_server.py`
- Modify: `pyproject.toml` (optional extra `mcp = ["mcp>=1.2"]`; pin the exact version present in `uv.lock` after `uv add --optional mcp mcp`)
- Modify: `src/litforge/cli.py` (`litforge mcp [--project ID]`, stdio transport)
- Modify: `docs/guide.md`, `docs/Guide_Fa.md` (a section on connecting Claude Code and other clients, for example `claude mcp add litforge -- litforge mcp --project <id>`)
- Test: `tests/unit/test_mcp_server.py`

**Interfaces:**
- Consumes: `retrieve_for_question`, `retrieve_broad`, `get_card`, `entity_papers`, `paper_neighbors`, the evidence ledger.
- Produces MCP tools. All of them are read-only and return JSON with `paper_id`, `title`, `page`, `evidence_id` and `text`, capped by a `max_tokens` argument (default 4000):
  - `search_evidence(query, k?, scope?)`
  - `get_chunk(evidence_id, context_chunks?)`
  - `get_paper_card(paper_id)`
  - `list_papers(query?, limit?)`
  - `find_entity(name)`
  - `paper_neighbors(paper_id)`
  - The server never calls `ModelGateway.call`. `search_query` translation is used only when its cache already holds the query; otherwise the English query is used as it is (Review Focus 5).

- [ ] **Step 1: Write the failing tests**: `test_tools_listed` (the six names); `test_search_evidence_respects_max_tokens`; `test_server_never_calls_gateway` (a gateway stub that raises on `call`; every tool runs successfully).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** with `mcp.server.fastmcp.FastMCP`; the database is opened read-only (`mode=ro` URI).
- [ ] **Step 4: Run** the tests; then connect a real client (Claude Code) to a sbi10 library and run three questions; record the tool calls and the token counts in the validation section.
- [ ] **Step 5: Commit** `feat(mcp): read-only MCP server over library search`.

---

## Validation record

For each task: the three rounds (run → inspect → diagnose → fix → rerun), with the commands, the benchmark numbers before and after, and the evidence tokens per question. Leave a round empty until it has actually run.

| Task | Round 1 | Round 2 | Round 3 | sbi10 Recall@10 | scale500 chunk / paper recall | Evidence tokens |
|---|---|---|---|---|---|---|
| 1 | — | — | — | 67% (baseline) | — | — |
