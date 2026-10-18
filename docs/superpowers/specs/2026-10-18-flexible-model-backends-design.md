# Flexible model backends: llama.cpp, per-task routing, capabilities, API providers — design

**Date:** 2026-10-18
**Builds on:** the hybrid model gateway (`src/litforge/analysis/gateway.py`, `src/litforge/llm/`) and every
feature that calls it (ask, audit, matrix, claims, relations, full-text screening, figures, gaps, console).

## Goal

Run LitForge on whatever the user's hardware and accounts allow: Ollama or llama.cpp locally, any
OpenAI-compatible API (OpenRouter first, also DeepSeek, Qwen/DashScope, Kimi/Moonshot, OpenAI, Anthropic and
Google through their OpenAI-compatible endpoints, a university vLLM), with a different model per task, image
input only where a model supports it, and reasoning switched on only for the tasks that need it — all through
the existing gateway, so privacy, approval, budget and audit rules stay exactly as they are.

## 1. Profiles gain capabilities (`ModelProfileConfig`)

New optional fields, all backwards compatible:

- `vision: bool = False` — the model accepts images. `call_vision` refuses a profile without it
  (`VisionUnsupported`) before any transport access. The built-in `local_vision` profile has `vision: true`.
- `reasoning: Literal["off", "on", "auto"] = "off"` — the profile's default; a task may override it (§2).
- `context_tokens: int | None` — when set, the gateway refuses a call whose estimated input + max output
  exceeds it (`GatewayError: request does not fit the model's context of N tokens`) instead of letting the
  server truncate. Evidence packets are trimmed first: features that build packets pass a budget and drop the
  lowest-ranked evidence until the request fits; the dropped IDs are recorded in the run.
  Ollama also receives this value as `options.num_ctx`. Built-in local profiles leave it unset: Ollama
  requests choose the smallest of 8192, 16384 or 32768 that fits estimated input + output cap + 1024 per
  image. Larger requests require an explicit context setting. This applies to JSON and page transcription;
  `options.num_predict` carries the output cap.
- `max_output_tokens: int | None` — default output cap for the profile; reasoning calls use
  `reasoning_output_tokens` (default 4096) because thinking tokens count against the cap.
- `structured_output: Literal["json_schema", "json_object", "none"] = "json_schema"` — what to send as
  `response_format`. `json_object` and `none` add the schema to the system message instead. In every mode
  LitForge validates the reply against the schema itself (already the case) and the gateway retries once
  on invalid JSON with a short "return only valid JSON for this schema" message (counted as a second call).
  Both clients check required keys and top-level JSON types, resolving local `$defs` references one level;
  schema-invalid JSON uses the same retry. Feature-level Pydantic validation remains responsible for deeper checks.
- `api_key_env: str | None` — now optional for `openai_compatible`: a local server (llama.cpp, vLLM without
  auth) sends no `Authorization` header. A remote profile (`remote: true`) still requires it.
- `extra_body: dict[str, Any] = {}` — merged into every request body (provider-specific options, e.g.
  OpenRouter's `provider` preferences). Never logged, never part of the input hash except via its
  canonical JSON, and may not override `model`, `messages`, `response_format` or `max_tokens`.
- `headers: dict[str, str] = {}` — extra non-secret headers (e.g. OpenRouter `X-Title: LitForge`); names
  containing `auth`, `token`, `apikey`, `secret`, `password`, `cookie`, `credential` or `session` after
  case folding and removing `-`/`_` are rejected, including nested `extra_body` keys. OpenRouter's `X-Title`
  and `HTTP-Referer` are allowed.
- `pricing_source: Literal["config", "openrouter"] = "config"` — see §4.

## 2. Per-task routing (`models.tasks`)

```yaml
models:
  tasks:
    ask:            {profile: gemma4, reasoning: on}
    audit:          {profile: gemma4, reasoning: on}
    claim_relation: {profile: cloud_reasoning, reasoning: on}
    matrix_extract: {profile: gemma4, reasoning: off}
    screen_fulltext:{profile: local}
    asset_analyze:  {profile: gemma4}
    console_plan:   {profile: gemma4}
```

- Task names are the gateway task strings already in use (`ask`, `audit`, `matrix_extract`,
  `claim_extract`, `claim_relation`, `screen_fulltext`, `asset_analyze`, `equation_compare`, `gap_design`,
  `console_plan`, and `page_rewrite` from §5). Unknown task names in config are a validation error that
  lists the valid ones.
- Resolution order for a call: explicit `profile` argument (CLI `--profile`, UI select) → `models.tasks[task]`
  → `local_vision` for image calls → `local`. The router's policy, sensitivity and approval checks apply to the
  resolved profile exactly as today; a task mapping to a remote profile under `local_only` is rejected, never
  silently swapped for a local one.
- `CallPreview` reports the resolved profile, its reasoning mode and whether it came from the task map.

## 3. Reasoning control per request

The gateway passes the effective reasoning mode (task override → profile default) to the provider:

- Ollama: `think: true|false` (currently always false).
- OpenAI-compatible: `reasoning` handling by profile setting `reasoning_param`:
  - `chat_template_kwargs` (llama.cpp / vLLM): `{"chat_template_kwargs": {"enable_thinking": bool}}`;
  - `openrouter`: `{"reasoning": {"enabled": bool}}` (OpenRouter's unified parameter);
  - `reasoning_effort`: `{"reasoning_effort": "low"|"medium"|"high"}` when on, omitted when off;
  - `none` (default for unknown servers): nothing is sent; the mode is only recorded.
- `auto` sends nothing (server default). Replies keep only `message.content`; a `reasoning_content` field is
  ignored and never stored. The model call record stores the effective mode.

## 4. Pricing from OpenRouter

`pricing_source: openrouter` fetches `https://openrouter.ai/api/v1/models` (public, no key) at most once a day
into `data/cache/http`, reads `pricing.prompt` and `pricing.completion` (USD per token) for the profile's
model id and converts to per-million. Failure to fetch, or an unknown model id, means "pricing missing", so the
budget guard blocks remote routing as today — never a guessed price. `litforge models pricing` shows the
prices in use and their source and age.

## 5. Page rewrite through the gateway

`parsing/vision.py` page transcription ("Math: high") currently calls Ollama directly. Route it through
`ModelGateway.call_vision` with task `page_rewrite` and a plain-text reply (new `call_vision_text` or a
`schema=None` mode returning text), so the same profiles, capabilities, privacy rules and audit apply. The
default remains the local vision model. Cached transcriptions are keyed by provider + model + prompt version,
so switching models re-transcribes only pages not yet done by that model. Full pages are images of a page, not
the PDF; sending them to a remote profile requires the same explicit approval as any cloud call, and the
preview states "N page images".

## 6. Ready-made profiles and commands

- `configs/models.example.yaml` documents ready-to-copy profiles: `llamacpp_local` (base_url
  `http://127.0.0.1:8080/v1`, no key, `reasoning_param: chat_template_kwargs`), `ollama_*`, `openrouter_*`
  (base_url `https://openrouter.ai/api/v1`, `api_key_env: OPENROUTER_API_KEY`, `pricing_source: openrouter`,
  `reasoning_param: openrouter`, `extra_body: {provider: {data_collection: deny, require_parameters: true}}`,
  header `X-Title: LitForge`), `deepseek`, `dashscope_qwen`, `moonshot_kimi`, `openai`, `anthropic_openai_compat`,
  `gemini_openai_compat`, `university_vllm`. Model names that change often are placeholders with a comment
  pointing to the provider's model list; base URLs are the providers' documented OpenAI-compatible URLs.
  Each remote profile has `remote: true`. A short table in docs/guide.md explains which to use for what.
- `litforge models list` — profiles, provider, model, remote, vision, reasoning, context, pricing status, and
  the task map.
- `litforge models test PROFILE [--vision]` — one tiny call (a fixed, non-library prompt and schema; with
  `--vision` a generated 64×64 test image) reporting reachability, key presence (never the key), JSON
  validity under the profile's `structured_output`, latency and token usage. A remote profile still needs
  `privacy.allow_cloud` and approval (`--approve`), and the call is audited like any other.
- `litforge doctor` additionally checks llama.cpp profiles via `GET /props` (if available): reports
  `modalities.vision` against the profile's `vision`, and `n_ctx` against `context_tokens`.

## 7. Safety invariants (unchanged, must be tested again)

No call reaches a transport before routing, cloud permission, sensitivity approval and budget checks. Keys are
read from environment variables at call time only; never stored, logged, shown or hashed. `extra_body` and
`headers` cannot carry credentials (validation rejects credential name fragments) and cannot override the
request's model/messages/schema/output cap. Remote profiles without pricing cannot be used under any policy.

## Testing

Fake transports only, no network: per-task resolution order and policy interplay; vision capability refusal;
context-fit refusal and evidence trimming; structured-output modes incl. retry on invalid JSON; reasoning
parameter per backend style; optional key for local and required for remote; extra_body/header validation;
OpenRouter pricing parsing from a recorded fixture, cache age, missing-model and fetch-failure paths;
page rewrite through the gateway with cache keys per model; `models list/test` output; doctor `/props` check
with a fake server. Full suite, Ruff, strict mypy, JS tests green.
