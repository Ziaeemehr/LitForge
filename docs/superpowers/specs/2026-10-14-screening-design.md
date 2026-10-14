# Title/abstract screening with a local LLM — design

Date: 2026-10-14 · Status: approved in chat, pending written-spec review

## Goal

Discovery runs (search, citations, import, similar-paper) return hundreds of candidates ranked by a
lexical relevance score and an importance score. Neither reads the paper's meaning against the user's
research question. Screening adds that step: a local model (Ollama) reads title + abstract of the top
N papers of a run and checks them against the project's research question and inclusion/exclusion
criteria. The user then sorts by the screening result, selects the top papers, and exports or
downloads them. The model only suggests; the user's ✓/✗ marks always override it. Nothing leaves the
machine.

This is Tier A of `docs/literature_analysis_architecture_plan.md` §7. Full-text screening (Tier B) is
out of scope.

## User-facing flow

1. Per project, the user fills a criteria form in the web UI: one research question, inclusion
   criteria (one per line), exclusion criteria (one per line).
2. On a run's page, the user presses **Screen** with a count N (default 100). The top N papers of the
   run by current priority are screened in a background job with progress, like other UI jobs.
3. The result list shows a decision badge (include / uncertain / exclude) and a screening score per
   paper; the paper drawer shows the rationale, per-criterion status and evidence quotes.
4. A new sort option **Screening** orders by decision group (include, uncertain, exclude, unscreened),
   then screening score, then priority. A filter by decision is added. A **Select top N** button uses
   the existing selection mechanism.
5. Markdown export includes the screening decision, score and reason.

Screening runs only when the user asks (button or CLI); never automatically after a search.

## Decision logic

Model input per paper: title, abstract, year, venue; the research question and criteria.

Model output (Ollama `format` = JSON schema, `think: false`, temperature 0):

```json
{
  "relevance": 0-5,
  "inclusion": [{"status": "met|not_met|unclear", "evidence": "short quote from abstract"}],
  "exclusion": [{"status": "violated|not_violated|unclear", "evidence": "short quote"}],
  "reason": "one or two sentences"
}
```

The arrays are positional, one entry per criterion in project order; the code pads or truncates to the
criteria count (missing entries become `unclear`).

Decision and score are computed in Python, never taken from model text:

- **exclude** if any exclusion criterion is `violated`;
- **include** if no inclusion criterion is `not_met` and `relevance >= 3`;
- **uncertain** otherwise. A missing abstract is screened on the title alone and can never be
  excluded: an `exclude` is downgraded to `uncertain` (architecture plan: missing abstract is not a
  negative result).
- **score** (0..1) = 0.5 · relevance/5 + 0.5 · inclusion fraction, where `met` = 1, `unclear` = 0.5,
  `not_met` = 0. With no inclusion criteria the score is relevance/5.

Evidence check: each non-empty evidence quote is checked against title + abstract after
whitespace/case normalization; quotes not found are flagged `unverified` and shown as such.

If the model returns invalid JSON, retry once; if it fails again, store decision `uncertain` with
`error` set and continue with the next paper.

## Reproducibility and caching

Each result stores the model name, a prompt version constant, and a fingerprint =
sha256(prompt version + model + research question + criteria + title + abstract). A paper whose stored
fingerprint matches is not screened again; changing the model, prompt or criteria re-screens it. A job
interrupted midway keeps the results written so far (one transaction per paper) and resumes on rerun.

## Components

New package `src/litforge/screening/`, one job per file:

| File | Responsibility |
|---|---|
| `llm.py` | `OllamaClient.chat_json(model, messages, schema) -> dict` over `urllib` (stdlib, like the rest of the UI); `LlmError`; a `FakeLlm` for tests |
| `prompt.py` | `PROMPT_VERSION`, `build_messages(criteria, paper)`, `response_schema(criteria)` |
| `decide.py` | `decide(raw, criteria, title, abstract) -> Decision` — pure rules, score, evidence check |
| `run.py` | `screen_run(conn, cfg, llm, project_id, run_id, top, progress, log) -> ScreenReport` |

Criteria type (in `screening/prompt.py` or a small `criteria.py`): `Criteria(question: str,
include: list[str], exclude: list[str])`, read from and written to `projects.research_question` and
`projects.criteria_json` (`{"include": [...], "exclude": [...]}`). Both columns already exist.

### Database — migration `0006_screening.sql`

```sql
CREATE TABLE screening_results (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id     TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    decision     TEXT NOT NULL,          -- include | uncertain | exclude
    score        REAL NOT NULL,          -- 0..1
    result_json  TEXT NOT NULL,          -- raw model output + per-criterion status + evidence flags
    error        TEXT,                   -- set when the model failed for this paper
    model        TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    screened_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, paper_id)
);
```

Results belong to (project, paper), not to a run, so a paper in several runs is screened once.

### Configuration

`ScreeningConfig` in `config.py` and `screening:` in `configs/default.yaml`:
`model: qwen3:4b`, `ollama_url: http://localhost:11434` (overridable by `LITFORGE_OLLAMA_URL`, which
`doctor` already reads), `default_top: 100`, `timeout_s: 120`.

### Web UI

- API: `GET/POST /api/criteria` (per project), `POST /api/screen` (starts a job: `run_id`, `top`).
- `api.run_results` joins `screening_results` and adds `screening: {decision, score, reason, ...}` per
  paper (null when unscreened); the paper detail endpoint returns full per-criterion results.
- `app.js`: criteria form in the project settings area; Screen button + count on the run view;
  decision badge in the list; drawer section; `screening` sort key; decision filter; Select top N.

### CLI

`litforge screen [--run RUN_ID] [--top N] [--model M]` — defaults to the active project's latest
run. Criteria are set only in the UI form; the CLI fails with a pointer to the form if none are set.

### Export

`export_markdown` adds `screening_decision`, `screening_score`, `screening_reason` to the front
matter and a Screening column to the README table when results exist.

## Errors

- Ollama unreachable or model not pulled: the job fails at start with a message naming
  `ollama pull <model>`; no papers are touched.
- No criteria set: `POST /api/screen` returns 400 asking to fill the form.
- Per-paper model failure: see Decision logic.

## Testing

- Unit (`tests/unit/test_screening.py`): decision rules (each branch, missing abstract, padding),
  score, evidence verification, fingerprint changes.
- Integration: `screen_run` with `FakeLlm` (writes results, skips cached, resumes); UI server endpoints
  (`/api/criteria`, `/api/screen`, screening fields in run results); export front matter.
- Optional live test marked to run only when Ollama answers and the model is pulled.

## Out of scope

Full-text (Tier B) screening, weighted multi-criterion rubric from the architecture plan, automatic
screening after search, cloud models, calibration of scores.
