# Settings page, API keys in the system keychain, and a cloud-light tier — design

**Date:** 2026-10-18
**Builds on:** docs/superpowers/specs/2026-10-18-managed-models-setup-design.md and
docs/superpowers/specs/2026-10-18-flexible-model-backends-design.md.

## Goal

Keep configuration out of the way of everyday work, let users enter API keys in the UI without the keys ever
touching LitForge's files, and give users without capable hardware a cheap, explicit cloud option for
extraction, images and analysis — with local screening still free and private.

## 1. Settings page

- A gear button **⚙ Settings** in the sidebar footer, next to Guide / theme / Quit; it opens a full-width
  `view-settings` (hash `#settings`), not a modal. The **Models** nav item is removed; its panel moves here.
- Sections, each a card with its own heading, in this order: **Models** (current setup, hardware, recommended
  tier, Apply, local server status, profiles with Test/Remove, task assignments), **API keys** (§2),
  **Add API model**, **Cloud & budget** (allow cloud on/off with the existing confirmation, policy, budget
  limits per call / run / month — editable, written to `<data>/config.yaml`), **Library & export** (contact
  e-mail and default export folder — moved from wherever they are today; behaviour unchanged).
- Anything that writes config shows the existing diff/confirmation. Keyboard help lists the new shortcut
  (`,` opens Settings, like many apps); works in both themes and at narrow width.

## 2. API keys in the system keychain (`litforge.llm.secrets`)

- Resolution at call time for a profile with `api_key_env: NAME`: environment variable `NAME` first, then the
  system keychain entry service `litforge`, account `NAME`. Nothing is cached in memory beyond the call.
- Backends, stdlib only:
  - macOS: `/usr/bin/security`; write with `security -i` reading the command from **stdin** (never the key on
    argv), `add-generic-password -U -s litforge -a NAME -w KEY`; read with
    `find-generic-password -s litforge -a NAME -w`; delete with `delete-generic-password`.
  - Windows: Credential Manager through `ctypes` (`CredWriteW`/`CredReadW`/`CredDeleteW`, target
    `litforge:NAME`, type GENERIC).
  - Linux: `secret-tool store/lookup/clear` (secret on stdin) when installed; otherwise the UI says keys must
    be set as environment variables.
- API: `GET /api/keys` returns, per known key name (from profiles and provider templates): `{name, source:
  "env"|"keychain"|"missing"}` — never the value or any part of it. `POST /api/keys` `{name, value}` stores
  it (same-origin, name must match `^[A-Z][A-Z0-9_]{2,63}$` and be a known key name); `DELETE` removes the
  keychain entry. The value is never logged, never echoed, never stored in config, DB, job reports or
  browser storage; the input is a password field cleared after save.
- CLI: `litforge models key set NAME` (prompts without echo), `models key remove NAME`, `models key status`.
- `models test` reports the key source ("env" / "keychain" / "missing").

## 3. Cloud-light tier

- New tier `cloud` in `models setup`, recommended when the hardware tier is `small` (and offered as an option
  for any tier). Plan:
  - screening (`screen`, `screen_fulltext`) → local Ollama `qwen3:4b` when Ollama is installed, else the
    cheap cloud model;
  - extraction and images (`matrix_extract`, `claim_extract`, `asset_analyze`, `equation_compare`,
    `page_rewrite`) → profile `cloud_cheap` (must support images);
  - analysis (`ask`, `audit`, `claim_relation`, `gap_design`, `console_plan`) → profile `cloud_strong`
    (or `cloud_cheap` if the user picks one model).
- Model choice from the live OpenRouter catalogue (the public models endpoint already used for pricing):
  `GET /api/models/catalogue?vision=1&max_input_price=…&q=…` returns id, name, context length, input/output
  price per million, `input_modalities`; cached daily like pricing. The Settings UI shows two pickers
  ("Cheap model for extraction and images — must accept images", "Strong model for analysis") with
  search, price, context and an "accepts images" badge, sorted by price. No model id is hard-coded.
  CLI: `litforge models catalogue [--vision] [--max-price X] [--search TEXT]` and
  `litforge models setup --tier cloud --cheap MODEL_ID --strong MODEL_ID`.
- The tier writes profiles via the OpenRouter template (privacy `data_collection: deny`,
  `require_parameters: true`, pricing from the catalogue), and sets `policy: ask_before_cloud` but leaves
  `allow_cloud` off until the user confirms "Enable cloud" (existing flow). It prints an estimate: cost of a
  typical matrix (papers × columns × average tokens) and of one page rewrite, from catalogue prices.
- Page rewrite via cloud sends whole-page images: the preview says "N page images will be sent", and it needs
  the per-run approval even under `hybrid`.

## 4. Safety invariants

Unchanged from the base specs, plus: key values never leave the keychain/env path (tests assert they appear in
no file under the data dir, no DB row, no log line, no API response, no job report); the keychain write never
puts the key on a process command line; catalogue data is untrusted (rendered with textContent, numbers
parsed defensively); cloud tier never enables cloud by itself.

## Testing

Inject keychain runners (fake `security`, fake ctypes layer, fake `secret-tool`) and HTTP fixtures (recorded
catalogue JSON). Unit: resolution order; macOS write uses stdin not argv; key name validation; GET never
returns values; deletion; cloud tier plan for small hardware with/without Ollama; catalogue filtering and
sorting; cost estimate arithmetic; preview wording for page images. UI tests: Settings view opens from the
footer and `,`, Models nav removed, sections present, key form clears, catalogue picker renders untrusted
names safely. Full suite, Ruff, mypy, JS tests green.

## 5. Choosing and downloading stronger local models

Users with stronger hardware pick a bigger local model, LitForge downloads it with progress, and it becomes a
profile assignable to tasks.

- **Find (live, nothing hard-coded):** `GET /api/models/local-search?q=…` calls the public Hugging Face API
  `https://huggingface.co/api/models?filter=gguf&search=Q&sort=downloads&limit=20`, then for a chosen repo
  `https://huggingface.co/api/models/REPO/tree/main` to list `.gguf` files with sizes and LFS sha256. For each
  repo show: downloads, publisher badge — **official** for `ggml-org`, `google`, `Qwen`, `meta-llama`,
  `mistralai`, `microsoft`, `openai`; **well-known quantizer** for `unsloth`, `bartowski`,
  `lmstudio-community`; otherwise **community — check the model card** (a warning; the UI links the card) —
  the quantization files (main model files only; `mmproj-*` marks image support, `mtp-*`/`dflash-*` and
  other auxiliary files are not offered as models), and image support.
- **Fit check:** memory budget = Apple unified RAM × 0.75, or NVIDIA VRAM (+ RAM for partial offload, shown as
  "slower: partly on CPU"), or RAM × 0.6 on CPU-only. Need ≈ file size + mmproj + KV cache estimate
  (context_tokens × 0.25 MB per 1k tokens as a stated rough rule) + 1 GB. Badges: **fits**, **tight**,
  **too large** (download still allowed after an explicit confirmation). Also checks free disk space.
- **Download:** LitForge downloads the selected file (and the matching `mmproj` if the user wants images)
  itself over HTTPS to `<data>/models/REPO/FILE`, with a background job: progress (bytes, speed, ETA),
  pause/cancel, resume via HTTP Range, sha256 verification against the LFS oid, then an atomic rename. Only
  `https://huggingface.co` URLs built from the API's repo/file names are fetched (no arbitrary URLs; repo and
  file names validated). A `HF_TOKEN` (env or keychain, §2) is sent only to huggingface.co, only when set.
- **Register:** on success it adds a preset section to `<data>/llamacpp-models.ini`
  (`model = /abs/path.gguf`, `mmproj = …` when chosen, `ctx-size`, `n-gpu-layers = 99`, `ubatch-size = 1152`
  when images) and a profile (`server: llamacpp`, `vision` from mmproj, `context_tokens`), and offers to assign
  tasks (checkboxes, same as API models). Removing the profile offers to delete the files.
- **Ollama users:** the same search box has an "Ollama" tab that pulls by name through Ollama's `/api/pull`
  stream (progress shown), then adds an `ollama` profile.
- CLI: `litforge models search TEXT`, `models files REPO`, `models download REPO FILE [--mmproj FILE]
  [--name NAME] [--task TASK]...`, `models pull-ollama NAME`.
- Tests: recorded HF API fixtures; fit-check arithmetic for several machines; download with a fake HTTP server
  (progress, resume after interruption, sha256 mismatch → file deleted, cancel), name validation rejecting
  path traversal and non-HF hosts, preset/profile registration, Ollama pull stream parsing.
