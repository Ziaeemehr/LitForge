# Claim Graph + full-text screening + active learning (roadmap phase 2) — design

**Date:** 2026-10-17
**Builds on:** evidence foundation (gateway, ledger, runs) and phase 1 (ask, audit, matrix, human_reviews).

## Goal

Cut decision time and reveal the real structure of a literature: which results are single-source, which are
replicated, where papers conflict and why that might be — and screen on full text with the person's time spent
on the borderline cases.

## Pass criterion

On a labelled set, the shortlist false-negative rate stays under a configured threshold
(`screening.max_false_negative_rate`, default 0.05), and no paper is excluded automatically without an explicit
violated exclusion criterion backed by verified evidence.

## 1. Claims (`litforge.analysis.claims`)

- `extract_claims(conn, gateway, project_id, paper_ids=None, profile=None, approved=False, force=False)`:
  per paper, retrieve the paper's result/conclusion/abstract chunks (sections whose path matches
  `abstract|result|finding|discussion|conclusion`, falling back to top FTS hits for the project question),
  record spans, one gateway call (task `claim_extract`, prompt `claims-v1`) per paper, schema
  `{"claims": [{"text": str, "type": "finding"|"method"|"hypothesis"|"limitation", "polarity":
  "positive"|"negative"|"null"|"mixed", "population": str|null, "outcome": str|null, "method": str|null,
  "evidence_ids": [str]}]}` (≤ 8 claims). Validation as phase 1: unknown IDs dropped; a claim without a valid
  ID is not stored (counted as `uncited_dropped`). Cache key per (paper current parse run, provider, model,
  prompt version).
- `canonical_text`: lowercase, unicode-normalised, whitespace-collapsed, trailing punctuation stripped;
  `canonical_hash` dedupes identical claims of one paper.
- Tables: `claims(id, project_id, paper_id, text, canonical_text, canonical_hash, type, polarity,
  population, outcome, method, status DEFAULT 'proposed' — proposed|accepted|rejected|edited, run_id, model,
  prompt_version, created_at, UNIQUE(project_id, paper_id, canonical_hash))`,
  `claim_evidence(claim_id, evidence_id, relation — supports|contradicts|qualifies, strength — direct|indirect,
  PRIMARY KEY(claim_id, evidence_id))`.

## 2. Clustering and relations (`litforge.analysis.claim_graph`)

- `cluster_claims(conn, project_id, threshold=0.55)`: deterministic. Similarity = cosine of TF-IDF vectors
  over canonical text (reuse `litforge.discovery.textsim` helpers), boosted +0.1 when outcome strings match
  (normalised) and capped at 1. Single-linkage over claims of **different** papers; each cluster gets an id and
  a label (the claim text of the most-cited member). Embeddings may replace TF-IDF later behind the same
  function signature (an optional `similarity` callable).
- `classify_relations(conn, gateway, project_id, max_pairs=200, ...)`: for claim pairs inside a cluster from
  different papers, one call (task `claim_relation`, prompt `relation-v1`) with both claims and their evidence;
  schema `{"relation": "supports"|"contradicts"|"qualifies"|"replicates"|"unrelated", "explanation": str,
  "possible_causes": [{"factor": "dataset"|"preprocessing"|"outcome_definition"|"design"|"population"|
  "other", "detail": str, "evidence_ids": [str]}]}`. Possible causes are stored as **hypotheses**; a cause
  without valid evidence IDs keeps the flag `unsupported_hypothesis`. Pairs ordered by similarity; budget
  errors stop the run cleanly.
- Deterministic pre-label: polarity positive vs negative/null on the same outcome → candidate `contradicts`
  (sent to the model first; never stored as final without the model or a human).
- Table `claim_relations(id, project_id, source_claim_id, target_claim_id, relation, explanation,
  causes_json, origin — model|human|rule, run_id, status DEFAULT 'proposed', created_at,
  UNIQUE(source_claim_id, target_claim_id))`, `claim_clusters(id, project_id, label, created_at)` and
  `claims.cluster_id`.
- Graph summary `claim_landscape(conn, project_id)`: per cluster — papers, number of supporting / contradicting
  papers, `single_source` flag, replication count, first paper by year (the claim's origin) and later papers
  citing it (via `paper_relations` when present), and **evidence strength** separate from citation popularity:
  `strength = n_independent_papers_supporting − n_contradicting`, shown next to `cited_by_count` sum, never
  merged into one score.

## 3. Full-text screening (S2/S3) (`litforge.screening.fulltext`)

- Stage S2 for papers with a current parse: per inclusion/exclusion criterion retrieve the top 3 chunks of that
  paper (query = criterion text + project question); one call per paper (task `screen_fulltext`, prompt
  `fulltext-v1`) with the same JSON shape as title/abstract screening but each evidence must be an evidence ID
  plus quote. Decision rules: same as `decide`, plus **exclude only when the violated criterion's quote is
  verified inside a cited span**; otherwise downgrade to `uncertain`.
- Stored in `fulltext_screening(project_id, paper_id, stage — 'S2', decision, score, result_json,
  evidence_ids_json, model, prompt_version, criteria_hash, fingerprint, run_id, screened_at,
  PRIMARY KEY(project_id, paper_id, stage))`.
- Every decision change is also appended to `screening_history(id, project_id, paper_id, stage, decision,
  source — model|human, model, prompt_version, criteria_hash, reason, created_at)` (append-only trigger); the
  title/abstract screener and the ✓/✗ marks in `paper_marks` append too (the UI's mark endpoint and
  `screen_run` write a history row). The history shows every reason and criteria version.
- Papers without a parse stay at S1 and are listed as `needs_pdf`.

## 4. Active learning queue (`litforge.screening.active`)

`review_queue(conn, project_id, limit=20) -> list[QueueItem]` ranks unmarked papers by an information score:

- **uncertainty**: `1 − |2·score − 1|` of the latest model screening (S2 over S1);
- **disagreement**: S1 and S2 decisions differ, or model decision contradicts the person's marks on the
  nearest marked papers;
- **boundary similarity**: TF-IDF similarity of title+abstract to the k=5 nearest ✓ and ✗ papers; high
  similarity to both sides scores high (`min(sim_pos, sim_neg)`);
- **low coverage**: no parse / no abstract.

`info = 0.4·uncertainty + 0.3·disagreement + 0.2·boundary + 0.1·coverage`; each item carries the components
and a human-readable reason. The queue never changes a decision; it only orders work. A simple kNN prediction
from marks (`predicted_relevant` probability) is shown next to the model decision but never overrides it.

## 5. False-negative evaluation

Benchmark extension: `screening:` list `{paper, relevant: true|false}`. `litforge eval screening BENCHMARK`
reports per stage: false-negative rate (relevant papers excluded), exclusion rate, uncertain rate, and the
shortlist recall at the current decision rules; fails with exit 1 when FN rate exceeds the configured
threshold. Stored as an analysis run.

## 6. CLI, UI

- CLI: `litforge claims extract [-p] [--paper ID]... [--profile] [--approve] [--preview] [--force]`,
  `claims cluster`, `claims relate [--max-pairs]`, `claims show [--cluster ID] [--json]`,
  `claims landscape [--json]`, `claims review accept|reject|edit ID`;
  `litforge screen --fulltext [-p] [--top N]`; `litforge queue [-p] [-n 20] [--json]`;
  `litforge eval screening BENCHMARK`.
- UI: new **Claims** view: clusters list with badges (single-source, replicated ×n, contradicted), cluster
  detail with member claims, relations and their hypothesised causes (labelled "hypothesis"), evidence cards
  (reuse the phase-1 card), accept/reject. Results view gains an **S2** badge and a **Review queue** sort
  (info score, with reason tooltip). Endpoints mirror the CLI under `/api/claims*`, `/api/queue`,
  `/api/screen-fulltext` (job).
- Export: `claims landscape` Markdown section appended to the research brief when `--claims` is passed.

## Migration 0011_claims_screening.sql (next free number)

Tables above; triggers make `screening_history` append-only.

## Testing

Fakes only. Unit: canonicalisation and dedupe; uncited claims dropped; clustering determinism and
different-paper rule; contradiction pre-label; causes without evidence flagged; landscape counts and
strength separate from citations; full-text exclude downgraded without verified quote; history rows from S1,
S2 and marks; queue scoring components and ordering; FN-rate evaluation and exit code. Integration: synthetic
PDFs → claims → cluster → relate → landscape; S2 screening → queue. UI endpoint tests. Full suite, Ruff, mypy.
