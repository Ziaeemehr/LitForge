# Managed llama.cpp server, hardware-based setup and API model selection — design

**Date:** 2026-10-18
**Builds on:** docs/superpowers/specs/2026-10-18-flexible-model-backends-design.md (profiles with capabilities,
per-task routing, reasoning control, OpenRouter pricing, `litforge models list/test`).

## Goal

Open LitForge and have the right models work without managing servers by hand: LitForge detects the machine,
proposes a model setup the user confirms once, starts and stops a local llama.cpp server itself, and lets the
user add API models (GPT, Claude, DeepSeek, Gemini, Qwen, Kimi — directly or through OpenRouter) as choices
per task. Nothing is downloaded, written or sent to the cloud without the user's confirmation.

## 1. User config file in the data directory

- `load_config(path=None, data_root=None)` merges, in order: `configs/default.yaml` → `<data_root>/config.yaml`
  (when it exists) → the explicit `--config` / `LITFORGE_CONFIG` file. CLI and UI pass their data root, so
  settings written by `models setup` / `models add` apply everywhere without environment variables.
- Writers (`models setup`, `models add`) update only the keys they own in `<data_root>/config.yaml`, keep the
  rest of the settings, write atomically (temp file + rename) and show a diff before writing.
- YAML comments are not preserved by the dependency-free writer. Before each config write, the exact previous
  file is copied to `config.yaml.bak`; the new file starts with:
  `# Managed in part by LitForge (models setup / models add). Comments elsewhere in this file are not preserved; the previous version is in config.yaml.bak.`
  The note is also shown in every CLI/UI file-change preview.
- `models.setup_owned: {profiles: [...], tasks: [...], server: llamacpp}` records setup ownership. Reapplying
  setup removes the previous owned entries before applying the tier, preserving user profiles and task mappings.
  `models add` releases ownership of profiles/tasks it replaces. Unrecorded entries are treated as user-owned.
  A tier that stops using the owned server stops it before removing its configuration.

## 2. Managed llama.cpp router (`litforge.llm.llamacpp_server`)

Config:

```yaml
models:
  servers:
    llamacpp:
      managed: true                 # LitForge may start/stop it
      command: llama-server         # resolved on PATH; absolute path allowed
      preset: llamacpp-models.ini   # relative to the data root
      host: 127.0.0.1               # loopback only; anything else is a validation error
      port: 8080
      models_max: 1                 # 1 = swap models on demand (least memory)
      idle_unload_minutes: 15       # passed as --sleep-idle-seconds
      start_timeout_s: 60           # process up and /health ok (model loading is separate)
```

- A profile links to it with `server: llamacpp`; its `base_url` defaults to `http://host:port/v1` and its
  `model` is the preset section name.
- `ensure_running(cfg)` is called by the gateway before dispatching to such a profile, after all routing,
  privacy and budget checks (it never runs for a call that would be rejected): GET `/health`; if it answers
  and `/v1/models` lists the profile's model → done. If something else answers on the port → `GatewayError`
  ("port 8080 is used by another server; change models.servers.llamacpp.port"). If nothing answers and
  `managed` → spawn `command --models-preset <preset> --models-max N --host H --port P --sleep-idle-seconds S`
  detached, stdout/stderr to `<data>/logs/llama-server.log`, record pid + start time in
  `<data>/state/llama-server.json`, wait for `/health` up to `start_timeout_s`. Missing executable →
  `GatewayError` with the install hint (`brew install llama.cpp`, or the llama.cpp releases page). Not managed
  and not running → `GatewayError` telling the user to start it.
- Model loading on first use can take minutes (and a first download much longer): the request timeout for a
  managed profile's first call after (re)start is `load_timeout_s` (default 900); the UI job shows
  "Loading <model>…" and `CallPreview` notes "starts llama-server; the model loads on first use".
- Stopping: LitForge stops only a server it started itself (pid file matches a live `llama-server` process
  whose command line contains the preset path) — on UI quit / idle shutdown and on `litforge models stop`.
  It never kills a server the user started. It checks pid, start time and preset command line again immediately
  before SIGTERM; after a five-second grace period it checks again before SIGKILL. The portable pid-based
  signalling API leaves a residual race between the final identity check and the signal.
- `litforge models start|stop|status` for manual control; human status is one line of preset aliases and load states.
  Extra router/Hugging Face cache entries and raw status details appear only with `--json`.
- Concurrency: one lock file around start so two LitForge processes never start two servers. OpenRouter
  catalogue check/fetch/write uses the same cross-process lock helper, with its own cache lock file.

## 3. Hardware detection and `litforge models setup`

`detect_hardware() -> Hardware` (stdlib only, each probe optional and time-limited):

- OS, CPU arch; total RAM (macOS `sysctl -n hw.memsize`, Linux `/proc/meminfo`, Windows
  `GlobalMemoryStatusEx` via ctypes); Apple Silicon → unified memory = RAM;
- NVIDIA: `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits` (largest GPU);
- installed tools: `llama-server` (and its version), `ollama`; free disk space in the data directory.

Tiers (thresholds in one table in code, documented in the guide; only models we have tested):

| Tier | Condition | Backend and models |
|---|---|---|
| large | Apple Silicon with ≥ 24 GB, or NVIDIA ≥ 16 GB VRAM and ≥ 32 GB RAM | llama.cpp router: `gemma4` = `ggml-org/gemma-4-26B-A4B-it-GGUF:Q4_0` (vision, reasoning, ctx 16384, `-b 2048 -ub 1152`) and `qwen3-4b` = `ggml-org/Qwen3-4B-GGUF:Q4_K_M` (ctx 16384); tasks: ask, audit, matrix_extract, claim_extract, claim_relation, gap_design, console_plan, asset_analyze, page_rewrite, equation_compare → gemma4; screen_fulltext and title/abstract screening → qwen3-4b |
| medium | ≥ 12 GB (unified or VRAM + RAM) | Ollama `qwen3:4b` for text, `qwen2.5vl:7b` for images |
| small | otherwise | Ollama `qwen3:4b` for text; image features off (`vision.model: null`, UI explains) |

- If the tier's backend is not installed, the proposal says so with the install command and offers the next
  tier that is installed; it never installs anything.
- `litforge models setup [--tier large|medium|small] [--yes]` prints detection, the proposal with approximate
  download and memory sizes, and the exact files it will write (`<data>/config.yaml` keys, and for large the
  preset `<data>/llamacpp-models.ini`); asks to confirm unless `--yes`. Downloads happen later, on first use
  of a model, and the preview of that first use says so.
- Web UI: the existing settings area gets a **Models** panel: detected hardware, current tier/profiles, the
  recommendation, an **Apply** button (same writer, with confirmation), and llama-server status
  (running/stopped/loaded model).

## 4. API models (`litforge models add`)

Built-in provider templates (base URL, key variable, reasoning parameter style, structured-output mode,
pricing source):

| Provider | base_url | key env | notes |
|---|---|---|---|
| openrouter | https://openrouter.ai/api/v1 | OPENROUTER_API_KEY | pricing from OpenRouter; `provider: {data_collection: deny, require_parameters: true}`; reasoning param `openrouter`; GPT, Claude, Gemini, DeepSeek, Qwen, Kimi by model id |
| openai | https://api.openai.com/v1 | OPENAI_API_KEY | json_schema; reasoning `reasoning_effort` |
| anthropic | https://api.anthropic.com/v1/ | ANTHROPIC_API_KEY | OpenAI-compatible endpoint; structured_output `json_object` |
| google | https://generativelanguage.googleapis.com/v1beta/openai/ | GEMINI_API_KEY | structured_output `json_object` |
| deepseek | https://api.deepseek.com | DEEPSEEK_API_KEY | structured_output `json_object` |
| qwen | https://dashscope-intl.aliyuncs.com/compatible-mode/v1 | DASHSCOPE_API_KEY | |
| kimi | https://api.moonshot.ai/v1 | MOONSHOT_API_KEY | |

- `litforge models add PROVIDER --model MODEL_ID [--name NAME] [--price-in X --price-out Y] [--task TASK]...
  [--vision] [--reasoning on|off]` writes a remote profile (`remote: true`) to `<data>/config.yaml`; model ids
  are always supplied by the user (no stale defaults); non-OpenRouter providers require prices (or the profile
  stays unusable and `models list` says "pricing missing"). `--task` maps tasks to it. It does **not** turn on
  `privacy.allow_cloud`; the command prints how to (`litforge models cloud on`, which sets
  `allow_cloud: true` and `policy: ask_before_cloud` after a confirmation explaining what leaves the machine).
- `litforge models remove NAME`, `litforge models cloud on|off`.
- UI Models panel: "Add API model" form (provider select, model id, optional prices, tasks), list of profiles
  with a Test button (`models test`, requires the separate cloud approval for remote profiles) and remove.
- The per-action model select in Ask/Matrix/Claims/Gaps/Console already lists profiles; remote ones keep the
  "(cloud)" label and the preview/approval dialog.

## 5. Safety invariants (unchanged, tested again)

No transport before routing/cloud/sensitivity/budget checks — and `ensure_running` only after them. Keys only
in environment variables, never written by `models add` (it writes the variable name), never logged or shown.
The managed server binds loopback only. LitForge never stops a process it did not start. Config writers never
drop unrelated keys.

## Testing

No network, no real llama-server, no real `nvidia-smi`: inject command runners, process spawner, HTTP probe
and clock. Unit: config merge order and atomic partial writes; tier selection for representative machines
(Mac 16/24/36 GB, Linux + NVIDIA 8/16/24 GB, CPU-only 8/32 GB) and missing-backend fallback; preset file
content; ensure_running states (already running, foreign server on port, missing executable, start timeout,
start success with pid file, lock); stop only own process; `models add` per provider (no key written, prices
required, tasks mapped); `cloud on` confirmation; gateway calls `ensure_running` only after checks. UI tests
for the Models panel endpoints. Full suite, Ruff, mypy, JS tests green.
