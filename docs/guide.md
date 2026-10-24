# LitForge guide

Details behind the [README](../README.md). Installation: [install.md](install.md).

## Command line

```bash
uv run litforge new my-review --topic "Attention mechanisms in deep learning"
uv run litforge add ~/papers
uv run litforge search "self-attention"
uv run litforge export
```

- `new` creates a project and makes it active, so later commands don't need `--project`.
- `project create NAME --topic "Topic" --question "Research question"` also creates and activates a project;
  both description options are optional. `project delete NAME` asks for confirmation (`--yes` / `-y` skips it).
  Papers and PDFs stay in the shared library; `--delete-exports` also removes that project's exported files.
- `desktop` opens LitForge in its own window (no browser); `--workspace FOLDER` chooses the library folder
  (remembered), `--gui` overrides the engine. One window per library: starting it again brings the window
  forward. Closing the window saves the open file and asks before cancelling running tasks.
- `desktop-setup` chooses and prepares the window engine (the installer runs it; no sudo): WebView2 on
  Windows, WebKit on macOS, the system's WebKitGTK on Linux, or Qt WebEngine (`--engine qt`) where WebKitGTK
  is missing. The choice is stored in `desktop.json` in the user config folder (`~/.config/litforge` on Linux).
- `shortcut` adds an application icon for LitForge's desktop window, including when its engine still
  needs setup. An engine failure shows recovery instructions instead of switching to a browser.
- `add` imports, parses and indexes PDFs in one step. Run it again any time; nothing is processed twice.
- `search` returns the paper, section, page and a quoted snippet for every hit.
- `ask "your question" --evidence-only` retrieves evidence cards with stable IDs, section and page.
  It stores full chunk snapshots and an analysis run; reindexing preserves the evidence. Add `--json`
  for the run ID and spans, `-k 8` to set the number of cards, or `-p NAME` to choose a project.
  Model-written answers arrive in phase 1.
- `export` writes Markdown notes to `~/Downloads/LitForge/my-review/markdown/` and each paper's PDF under a readable name to `~/Downloads/LitForge/my-review/pdfs/` (set `LITFORGE_EXPORT_ROOT` to change the base folder, or pick a folder per project in the web UI); `--only relevant` keeps just the papers marked ✓ (`--only not-irrelevant` drops the ✗ ones).

For an Obsidian vault, run `uv run litforge export --obsidian` (or select **Obsidian links** in the export header). Notes gain `litforge/<status>` tags and title aliases; the README and related notes use `[[slug]]` links. Related notes cite each other or share at least three references, and links stay within the exported selection. Add `--json` to print the export path and paper count as JSON. Without `--obsidian`, the Markdown export stays unchanged.

## Settings, keys and cloud-light

Open **⚙ Settings** in the sidebar footer, or use **`,`** outside a text field.
This full-width page has cards for Updates, Models, API keys, Add API model, Cloud & budget, and Library & export.
Settings has four tabs: General, Models, Tools, and Cloud & keys.

**Models → Mode** is one choice for which model does what:

| Mode | What runs where |
|---|---|
| Local | every task on this computer, with the local models of its tier; nothing is sent anywhere |
| Cloud | every task on cloud models: a cheap one for screening, extraction, figures and Better math (high), a strong one (optional) for questions, analysis and writing |
| Hybrid | screening and extraction (and Better math) on this computer; questions, analysis and writing on a cloud model |
| Manual | your own model for each task (**Assign tasks**); assigning a task by hand switches to Manual |

Cloud and Hybrid still need the one-time **Enable cloud** consent, keep the dollar limits, and always ask before
sending your own drafts. In Cloud, the Export menu's **Math: high** says it runs on the cloud and what a page costs.
The local tier (large / medium / small) is under **Advanced**. Model configuration writes show the exact diff and
ask for confirmation. YAML comments are
not preserved; the previous config is copied to `config.yaml.bak` as described below.

**Library & export** contains the contact e-mail used for OpenAlex, Crossref and Unpaywall, and the default
export folder. These retain the existing local `settings.json` settings behavior. A project's saved export
folder continues to take precedence; `LITFORGE_EXPORT_ROOT` overrides the default folder and
`LITFORGE_MAILTO` overrides the contact e-mail. Cloud permission, policy and per-call/run/month budget limits
are editable in **Cloud & budget**, saved in `<data>/config.yaml` and checked on every model call.

### API keys

Enter the provider's named key in **API keys**. The password field clears immediately after submission;
the UI shows only each key's name and source (`env`, `keychain`, or `missing`). Environment variables win over
keychain entries. Removing a keychain entry leaves any environment variable set. Keys resolve for each
provider call; LitForge does not save their values in its configuration, database, browser storage, logs or
job reports. `HF_TOKEN` is optional for public Hugging Face searches and downloads.

```bash
litforge models key set OPENROUTER_API_KEY  # hidden prompt, no key on the command line
litforge models key status
litforge models key remove OPENROUTER_API_KEY
litforge models test YOUR_PROFILE --approve  # reports key source; cloud test needs separate approval
```

macOS uses the login keychain through `/usr/bin/security` (writes send commands on stdin). Windows uses
Credential Manager. Linux uses `secret-tool` when available; otherwise set keys as environment variables.
LitForge installs no keychain tools. A keychain lock or access denial appears as **keychain error** in status and model tests; unlock it in the OS.
Removing an absent keychain entry succeeds and reports **No keychain entry for NAME.**

### Cloud-light for smaller machines

Cloud-light is offered on every machine and recommended when hardware is classified as small. Search the
live OpenRouter catalogue in Models and choose **Cheap model for extraction and images — must accept
images**, plus an optional **Strong model for analysis**. Pickers show model IDs/names, prices per million
tokens, context lengths and image support. Only models with text output are offered; paid models are sorted
by input price, with `:free` variants last. Free variants are labelled **free — often rate-limited; may be
unavailable with data_collection: deny**. There are no default cloud model IDs.
The public catalogue shares the daily pricing cache. CLI equivalents:

```bash
litforge models catalogue --vision --max-price 1 --search YOUR_SEARCH
litforge models catalogue --search YOUR_SEARCH
litforge models setup --tier cloud --cheap YOUR_VISION_MODEL_ID --strong YOUR_ANALYSIS_MODEL_ID
litforge models cloud on
```

Omit `--strong` to use one model for everything. Screening (`screen`, `screen_fulltext`) uses local Ollama
`qwen3:4b` when installed; otherwise it uses `cloud_cheap`. Extraction, asset/equation analysis and page
rewriting use image-capable `cloud_cheap`. Ask, Audit, claim relations, gap design and console planning use
`cloud_strong`, or the cheap model when only one was chosen. Existing user-owned task assignments survive
setup. Both cloud profiles use OpenRouter with `data_collection: deny` and `require_parameters: true`.

The proposal estimates a typical matrix (20 papers × 10 columns, 1000 input and 100 output tokens per cell)
and one page rewrite (1024 image-input estimate and 4000 output tokens), using catalogue prices. Actual cost
varies with evidence, images and output. Setup sets `ask_before_cloud` and leaves cloud **off**; enabling it
requires the separate **Enable cloud** confirmation. Whole-page image rewrites always require per-run
approval, even with `hybrid` policy. The export preview says **N page images will be sent** and shows an
estimated maximum cost; when fetching PDFs too, they are fetched before counting the images for approval.

### Finding and downloading stronger local models

In **Settings → Models → Find stronger local models**, search public Hugging Face GGUF repositories. Each
result has downloads, a publisher badge and a model-card link. Repositories indicating embeddings, reranking,
speech, image generation or classification are hidden by default; **Show all** (CLI `models search TEXT
--show-all`) includes them with a **not a chat model** label. Badges identify official publishers, well-known
quantizers, or **community — check the model card**; they are not a guarantee of model quality. Choose a
quantization and, for images, its matching `mmproj` (check the card). Projectors and auxiliary MTP/draft files
are excluded from the main model choices. No Hugging Face key is required for public models.

Fit badges are rough: model + projector sizes + 0.25 MB KV cache per 1000 context tokens + 1 GB overhead.
Apple's budget is 75% unified RAM; CPU-only is 60% RAM. NVIDIA first uses VRAM, then 60% RAM for partial
offload, marked **slower: partly on CPU**. A tight badge means the last 10% of the available budget. **Too large** needs
an additional explicit confirmation; insufficient free disk blocks the download. Choose context size and
optional task checkboxes before reviewing the download and config diff.

LitForge downloads to `<data>/models/PUBLISHER/REPO/FILE.gguf.part`, shows bytes/speed/ETA and supports
**Pause**, **Resume** and **Cancel download**. Terminal progress updates one line with whole-second/minute
ETA; redirected output reports at most once per 5% step. The per-file lock is removed on completion or exit. After an interruption, Resume or repeating the download resumes
with HTTP Range. SHA-256 must match the Hugging Face LFS oid before atomic publication as `.gguf`; a checksum
mismatch deletes the partial file. Download requests start only at validated Hugging Face HTTPS paths;
Hugging Face's HTTPS weight-CDN redirects are restricted to its domains and never receive `HF_TOKEN`.
A configured token is sent only to `huggingface.co`.

On success, the reviewed change adds the absolute `model` (and optional `mmproj`) paths to
`<data>/llamacpp-models.ini` and a local llama.cpp profile with the chosen context and image capability.
Updating downloaded presets stops only LitForge’s owned router, so it reloads the preset on next use.
LitForge starts the managed router when needed; install llama.cpp yourself if it is missing. Profiles can be
assigned with the task checkboxes. Removing a downloaded profile offers to delete its owned model files;
files referenced by another preset are retained. The **Ollama** tab pulls an exact name through local
Ollama's streaming API, shows progress and offers to register an Ollama profile. CLI equivalents:

```bash
litforge models search YOUR_SEARCH
litforge models files PUBLISHER/REPO
litforge models download PUBLISHER/REPO MODEL.gguf --mmproj MMPROJ.gguf --name chosen --task ask
litforge models pull-ollama YOUR_OLLAMA_MODEL_NAME
litforge models remove chosen --delete-files
```

Downloads ask before starting. Ctrl+C pauses CLI downloads; repeat the command to resume. Existing verified
files are reused. Setup still performs no downloads until first use, and no workflow installs tools.

## Choosing models

Defaults stay local: Ollama `qwen3:4b` for text and `vision.model` for images. Copy profiles from
[models.example.yaml](../configs/models.example.yaml), then run `litforge --config YOUR.yaml models list`.

Run `litforge models setup` (or open **⚙ Settings → Models** in the web UI) to detect hardware and review a setup.
Nothing is installed or downloaded during setup. It shows the hardware, approximate sizes, exact paths and
file diff, then asks before saving. `--tier large|medium|small|cloud` overrides the recommendation; `--yes` confirms
the proposed files without a prompt. Missing backends are reported with installation instructions; when
llama.cpp is missing and Ollama is installed, the large recommendation offers the medium tier instead.

| Tier | Hardware condition | Setup | Approximate download / memory |
| --- | --- | --- | --- |
| large | Apple Silicon with at least 24 GB unified RAM, or NVIDIA with at least 16 GB VRAM and 32 GB RAM | llama.cpp router: Gemma 4 26B A4B Q4_0 for analysis and images; Qwen3 4B Q4_K_M for title/abstract and full-text screening | 20 GB / 24 GB |
| medium | At least 12 GB unified memory, or NVIDIA VRAM + RAM totalling at least 12 GB | Ollama `qwen3:4b` for text, `qwen2.5vl:7b` for images | 8 GB / 12 GB |
| small (optional local tier) | Other machines, including CPU-only machines | Ollama `qwen3:4b`; image features off | 3 GB / 4 GB |

These are estimates, in binary GB (GiB), for the tested model choices. Large uses a 16384-token context,
Gemma batch 2048 and ubatch 1152 (required for the full image budget), and one resident model at a time.
Ask, Audit, Matrix, Claims, Relations, Gaps, Console, asset analysis, page rewriting and equation comparison
use Gemma; screening uses Qwen. Hardware classified as small, including CPU-only systems with ample RAM,
is offered cloud-light by default; the explicit small tier remains available for local-only use.

Settings are saved in `<data>/config.yaml`. Loading merges `configs/default.yaml`, this user config, then
`--config FILE` (or `LITFORGE_CONFIG`), so explicit overrides still win. Writers preserve unrelated keys and
publish each file atomically. Existing users who never run setup retain the original defaults.

Large also writes `<data>/llamacpp-models.ini`. LitForge starts `llama-server` on loopback on the first
accepted call, with `--models-preset`, `--models-max 1` and a 15-minute idle-unload timer. Model weights download
and load on first use; previews say so, and first-load requests allow 900 seconds by default
(`load_timeout_s` on the profile). Ollama setup models are also pulled on first use if missing; Ollama must
already be running. Setup never installs either backend. CLI controls are:

```bash
litforge models start
litforge models status  # includes router models and their loaded/unloaded status
litforge models stop
```

UI Quit, closing all tabs until the idle timeout, and Ctrl+C stop only a server LitForge started, verified
by PID, process start identity and preset command argument. An external server is reused when its router lists
the requested model and is never stopped. Conflicting listeners produce a port-setting error. See
`models.servers.llamacpp` in [models.example.yaml](../configs/models.example.yaml) for the server settings.

Managed llama.cpp setup uses port **8093** and a **600 second** request timeout per profile
(`timeout_s`; the default for other profiles is 120). First model loading uses `load_timeout_s`.
The large tier starts with reasoning off for every task. You can enable it per task with
`reasoning: "on"`; local llama.cpp profiles default to an 8192 token reasoning output allowance.
Turning reasoning on for long-evidence tasks can cost **minutes per call**, including retries.
Image calls without an explicit profile or task assignment use `models.vision_profile` when set;
otherwise they use the Ollama model in `vision.model`. Large setup sets `vision_profile: gemma4`
and leaves `vision.model` untouched. Title/abstract screening uses the `screen` task assignment;
without one it keeps the existing `screening.model` default, and `screen --model` overrides it.
`models setup`, `models status` and `models list` print readable summaries; add `--json` for full JSON.

Add an API model using its exact current model ID and prices (USD per million tokens):

```bash
litforge models add openai --model YOUR_MODEL_ID --name api_text --price-in 1 --price-out 4 --task ask
litforge models add openrouter --model PROVIDER/MODEL_ID --name api_choice --task matrix_extract
litforge models cloud on
litforge models test api_text --approve
litforge models remove api_text
litforge models cloud off
```

Providers: `openrouter`, `openai`, `anthropic`, `google`, `deepseek`, `qwen`, `kimi`. Optional `--vision` and
`--reasoning on|off` set capabilities. Non-OpenRouter profiles without both prices remain unusable; OpenRouter
fetches current prices. Set the named provider key in your environment or **Settings → API keys**, never in YAML. Adding a model
does not enable cloud access. `models cloud on` separately explains the evidence/images that leave the machine
and asks for confirmation, then sets `allow_cloud: true` and `policy: ask_before_cloud`. Every remote diagnostic
also needs its own approval. The Settings Models card offers the same review/apply flow, API form and profile tests.
Per-action profile menus keep the cloud label and approval preview.

| Provider | Use it for |
| --- | --- |
| Ollama | Simple local text and vision models; pull models with Ollama first. |
| llama.cpp | Local GGUF models and thinking control; managed setup uses loopback port 8093. |
| OpenRouter | A choice of hosted models with automatic daily pricing. |
| DeepSeek | Hosted text extraction and reasoning. |
| Qwen / DashScope | Hosted Qwen models; choose the endpoint matching your key's region. |
| Kimi / Moonshot | Hosted Kimi models. |
| OpenAI | Hosted models with schema output and reasoning effort control. |
| Anthropic compatibility | Claude through its OpenAI-compatible endpoint; use `structured_output: none`. |
| Gemini compatibility | Gemini through its OpenAI-compatible endpoint. |
| University vLLM | An institution's model server; mark it remote if evidence leaves your computer. |

Set `models.tasks` to choose a profile, optional reasoning mode and `max_output_tokens` per task. Explicit
`--profile` or UI choices
come first, then the task map, then `local` (or `models.vision_profile` for images, falling back to
`local_vision`). Mark image models `vision: true`.
Reasoning defaults to off; on uses `reasoning_output_tokens` (8192 for local llama.cpp, 4096 otherwise). Set `context_tokens` to match
your server: evidence is trimmed in rank order, dropped IDs are recorded, and requests that cannot fit are refused.

Remote profiles require `remote: true`, an `api_key_env`, current per-million prices (or
`pricing_source: openrouter`), cloud permission, and a permitting policy. `litforge models pricing` shows source
and cache age. `litforge models test PROFILE [--vision] [--approve]` sends a fixed diagnostic prompt;
remote tests require approval. `litforge enhance --preview` counts uncached page images;
`--profile` and `--approve` select and approve remote transcription. `litforge doctor` checks local llama.cpp
capabilities and context. Provider documentation links are beside the profiles in the example config.

Model setup records the profiles, task assignments and server it owns in `models.setup_owned`.
Switching tiers removes those entries and stops an owned llama.cpp server when the new tier does not use it.
User profiles and task assignments are retained; assigning a task with `models add` releases it from setup ownership.
Existing entries without an ownership record are treated as user settings.

Model configuration writes save the exact previous file as `config.yaml.bak` and add a header explaining
that YAML comments are not preserved. The CLI and Settings page show this note with the proposed file diff.
Human server status shows one line with only preset model names and load states; use `models status --json`
for the raw router response. When `models.vision_profile` selects a replacement, the unused built-in
Ollama vision profile is hidden from the model list.


## Relevance, importance and review priority

Every result gets two scores, both between 0 and 1:

- **Relevance** to your seed or query: meaning (embeddings), shared references, citation links,
  agreement between search channels.
- **Importance**, independent of your query: field- and year-normalized citation percentile
  (OpenAlex), how many papers in the same result set cite it (core literature), citations in the
  last two years, and the venue's 2-year mean citedness. Retracted papers get 0 and a warning.

**Review priority** = 0.65 × relevance + 0.35 × importance, the default sort order, so a large
result set can be read from the top.

## Screening with a local model

Relevance counts shared words and citations; it does not read the paper. Screening does: a local
model (Ollama, default `qwen3:4b`) reads the title and abstract of a run's top papers and checks them
against the project's research question and inclusion/exclusion criteria. It suggests **include**,
**uncertain** or **exclude** with a reason and quotes from the abstract (quotes not found in the
title or abstract are flagged). A violated exclusion criterion requires a verified quote to exclude;
otherwise the decision is uncertain with the reason “exclusion quote not verified”. The decision and a
0–1 score are computed by fixed rules, not by the model; a paper without an abstract is never excluded. Your ✓/✗ marks are separate and always decide.
Nothing leaves your computer.

1. `ollama pull qwen3:4b` once (`qwen3:8b` is slower but more careful: set `screening.model`).
2. In the web UI open a run, fill in **Screening** (question, criteria one per line), press **Screen**.
   About 2 s per paper on an RTX 5070 with `qwen3:4b`.
3. Sort by **screening**, filter by decision, **Mark top N ✓**, then export the ✓ papers.

Changing the criteria or the model marks earlier results as stale; screening again re-checks them,
and unchanged papers are not sent to the model twice.

### Model gateway and cloud policy

LitForge's model layer can also target an OpenAI-compatible endpoint, including a university-hosted
vLLM server. Cloud use is disabled by default. The gateway routes only explicit evidence packets,
never complete PDF bytes, and supports hard per-call, per-run, and monthly budgets. API keys are read from
environment variables named by the configuration, then from the system keychain. Their values are not stored
in LitForge files, database rows, logs or job reports.
Named remote profiles can be added under `models.profiles`; each profile declares its endpoint, model,
and API-key environment-variable name. No remote profile is configured by default.

The policies are local_only, ask_before_cloud, and hybrid. Remote fallback is never silent:
sensitive evidence and ask_before_cloud require explicit approval from the calling feature. The current
screening UI continues to use Ollama by default; cloud-backed research features will opt into the
gateway in later phases.

Remote profiles need `pricing.input_per_million_usd` and `pricing.output_per_million_usd` before
the gateway can dispatch. `litforge calls --limit 20` shows the audit log; `--run RUN_ID` filters
one analysis run, and `--json` includes the recorded fields.

### Measure retrieval

```bash
uv run litforge eval retrieval configs/benchmark.example.yaml -k 10 --json
```

Copy [benchmark.example.yaml](../configs/benchmark.example.yaml) and label questions against your
own indexed papers. Use a paper ID, DOI or arXiv ID; optionally require a PDF page and a substring
of the evidence text. Each question needs a unique ID and at least one target. YAML and JSON both
work. A useful first benchmark has 30 questions from your reading, with the pages and passages you
would expect to find; the example uses synthetic test papers.

The command stores a run and reports mean Recall@k (fraction of targets found per question),
hit-rate@k (questions with any target found), MRR (reciprocal rank of the first relevant hit), and
missed targets. Retrieval currently uses lexical OR search over content words and quoted phrases.
Keep the same benchmark when comparing future retrieval methods.

### The sbi10 gold benchmark

[benchmarks/sbi10](../benchmarks/sbi10/README.md) is a labelled benchmark on ten open-access arXiv
papers, pinned by version and file hash: 30 English and 20 Persian questions with 88 page-and-quote
targets, a 10-paper study matrix, screening labels and 14 claims with 7 relations between papers.
Quotes are compared without case, ligatures, line-break hyphens or extra whitespace.

```bash
uv run python scripts/fetch_benchmark.py --ids-file benchmarks/sbi10/papers.txt --out data/benchmark/sbi10
uv run litforge new sbi10 && uv run litforge add data/benchmark/sbi10/pdfs
uv run litforge eval check benchmarks/sbi10/benchmark.yaml     # every labelled quote is in the index
uv run litforge eval all benchmarks/sbi10/benchmark.yaml --preview
uv run litforge eval all benchmarks/sbi10/benchmark.yaml --approve --out sbi10-report.md
```

`eval all` runs the label check, retrieval, answers, the matrix, full-text screening and the claim
graph in turn, and writes one Markdown report that compares each metric with the roadmap's targets.
A stage that fails is reported and the others still run; `--skip-models` stops after retrieval, and
`--force` redoes cached matrix cells, screening decisions and claims (after a reindex or a model change).
It sets the project's screening criteria and creates a matrix named `sbi10-gold`, so use a project
of its own. `litforge eval claims` scores claim recall and relation accuracy on their own.

## Find papers

```bash
uv run litforge discover "self-attention"            # search OpenAlex, Crossref and arXiv
uv run litforge discover --seed 10.48550/arXiv.1706.03762   # follow references and citing papers
uv run litforge import refs.bib                      # add a .bib, .ris or .csv reference list
uv run litforge enrich                               # fill in missing DOI, year, venue, abstract
uv run litforge papers --no-pdf                      # ranked list of papers still missing a PDF
uv run litforge fetch                                # download open-access PDFs of the ✓ papers
```

`fetch` downloads a legal open-access copy (arXiv, or the publisher's open-access URL; on a
landing page it follows the `citation_pdf_url` link), attaches it to the existing record, then
parses and indexes it like `add`, so exports contain the full text. `--only not-irrelevant` or
`--only all` widens the selection. Paywalled papers are reported and skipped; add their PDFs with
`add`.

In the web page, **+ New → My sources** also takes:

- **Links to articles**, one per line: the article's own page (its DOI is read from the tags publishers add for
  indexers, never from the page's reference list), a `doi.org`, arXiv or OpenAlex link, or a direct PDF link. Each
  paper joins the project and its open-access PDF is downloaded and read; a paywalled paper keeps its abstract.
- **Documents other than PDF**: Word (`.docx`), PowerPoint, Excel, HTML, EPUB, Markdown and text files, read with
  Microsoft's MarkItDown (the `documents` extra, part of the standard install) into the same structure as a PDF, so
  search, export and the AI features work on them. A document has no pages: its quotes point to the file. Export
  copies it beside the notes under its own extension.

### Papers similar to a paper you have

```bash
uv run litforge similar 10.7554/eLife.56261           # by DOI (or arXiv id, or a PDF file)
uv run litforge similar --abstract-file abstract.txt  # from an abstract only
```

Candidates come from the seed's references, papers citing it, OpenAlex related works and
searches built from its key phrases (for an abstract-only seed, also the citations of the closest
matches). They are ranked by text similarity, shared references and citation links, and each
result says why it was found. Meaning-based similarity uses a small embedding model that runs on
a laptop CPU (`uv sync --extra embed`; downloaded once to `data/cache/models`). On a GPU machine
set `similar.embedding_model` (e.g. `BAAI/bge-large-en-v1.5`) and `embedding_device: cuda` in the
config.

Results from all sources are merged (by DOI, arXiv id, OpenAlex id, then title and year),
ranked by relevance, and added to the active project. Every run is recorded together with why
each paper was found. When you later `add` the PDF of a discovered paper, it is attached to the
existing record. `discover` without a query uses the project topic.

API responses are cached for a week in `data/cache/http`. OpenAlex meters usage: without an API
key there is a small free daily budget (about 100 searches; single-paper lookups are free). Set
`LITFORGE_OPENALEX_API_KEY` to use your own key and `LITFORGE_MAILTO` to identify yourself to
OpenAlex and Crossref (both optional).

Other useful commands:

```bash
uv run litforge status            # what is in the active project
uv run litforge project list      # all projects (* marks the active one)
uv run litforge project use NAME  # switch the active project
uv run litforge doctor            # check the environment
uv run litforge screen --top 100  # screen the latest run's top papers (criteria are set in the web UI)
```

Any command accepts `--project NAME` to work on a project other than the active one.
`litforge --help` lists everything, including the individual `ingest`, `parse` and `index`
steps that `add` combines.

## Research Console

The Console plans requests using a closed set of typed research actions. The planner receives only your
request and the action schema, never paper text. Review its per-step preview before approving execution:

```bash
litforge do "Compare study methods published since 2020" -p my-review
litforge do "Take a snapshot, then answer the project question" -p my-review --yes
litforge workflow run systematic_review -p my-review --param question="What methods are reported?"
litforge console runs -p my-review
litforge console resume RUN_ID
litforge console cancel RUN_ID
```

`--yes` approves execution only; a cloud model route requires a separate approval (`--approve-cloud` or
the interactive prompt) and remains subject to the configured gateway policy and budget. Each completed
step is checkpointed; a failed or cancelled run can resume from its next uncompleted step. Exports created
by console actions stay inside the project's export folder. The local web UI has a **Console** view with
the same plan preview, explicit Run confirmation, progress, cancellation and resume controls.

## Write (LaTeX documents)

**Write** in the sidebar holds the current project's LaTeX documents. Each document is a plain folder,
`<data>/projects/<project>/writing/<name>/` (`main.tex`, `references.bib`, `figures/`), so you can also open it
in another editor. Start from the **English article** or **Persian article** template; the Persian one uses
`xepersian` with the Vazirmatn font (shipped in the document's `fonts/` folder).

- **Editing.** The editor (CodeMirror) highlights LaTeX, searches (Ctrl/Cmd+F) and shows Persian lines right to
  left. It saves 2 seconds after you stop typing and when you leave the editor. If the file changed in another editor
  or editor, a banner asks whether to reload that version or keep yours; nothing is overwritten silently.
- **Versions.** A version is kept at most every 2 minutes while you save (the last 30 are kept; named versions are
  never removed). **Versions** lists them with the lines added and removed, shows what changed in one (against the
  version before it, the current text or any other version), saves a named version now (*Before submission*), and
  restores one; the current text is kept as a version first.
- **Text ↔ PDF.** Double-click a place in the PDF to open its line in the editor; **Find in PDF** (Ctrl/Cmd+Alt+J)
  scrolls the PDF to the cursor's line and highlights it. The PDF pane zooms and fits its width.
- **Spelling.** English and Persian words are checked as you write (commands, citation keys, labels, math and
  comments are skipped); right-click an underlined word for suggestions, *Add to my dictionary* or *Ignore*. The
  **Spelling** button turns it off. Persian words joined with a half-space pass when every part is a word.
- **Outline** opens the sections of the open file beside the editor (the one the cursor is in is highlighted);
  click one to jump there, or an included file (`\input`) to open it. The toolbar shows the file's **word count**:
  the text only, without commands, math, citations and comments, Persian words included.
- **Symbols** opens a searchable palette of math symbols (Greek letters, relations, arrows, operators, accents…):
  type `alpha`, `leq` or `arrow` and click one (or press Enter for the first). Inside math the command is inserted;
  elsewhere it comes wrapped in `$...$`.
- **Previews.** Rest the pointer on math (`$...$`, `\[...\]`, `equation`, `align`…) to see it rendered, with your own
  `\newcommand`s; what the fast renderer cannot draw is compiled with the document's engine. Rest it on an
  `\includegraphics` to see the figure (PNG, JPEG or PDF).
- **Notes.** Click the narrow column left of the line numbers to write a note on that line: a reminder for yourself,
  never compiled or exported. Lines with notes show a dot; the notes are also listed under **Outline**. A note
  follows its line when you edit around it.
- **More** holds the compile settings: the **main file** (any `.tex` with a `\documentclass`, for a thesis or an
  Overleaf project whose file is not `main.tex`), the **engine** (Tectonic, or XeLaTeX from TeX Live via `latexmk`)
  and **Draft compile** (one engine pass: faster while writing, but references may show `??` until a full
  compile). **Download as ZIP** saves the document's files (without versions or build output).
- **Import ZIP** on the document list makes a new document from a `.zip`: an Overleaf download, a journal template
  or a LitForge export. The main file is found by its `\documentclass`, and cited papers go into the `.bib` file the
  main file names (`\bibliography{refs}` → `refs.bib`). ZIPs of at most 100 MB, 2000 files and 200 MB unpacked are
  accepted; links and paths outside the document are refused.
- **Git sync** (in **More**) keeps a document in a git remote, both ways: Overleaf's git link, GitHub, GitLab or a
  bare repository on a shared drive. **Connect** a remote, then **Sync now** saves, commits, pulls the other side's
  changes and pushes yours; a version is kept before every sync. To work on an existing Overleaf or GitHub project,
  use **Clone from git** on the document list. When both sides changed the same lines, the editor lists the
  conflicting blocks: keep yours, theirs or both for each, then **Sync** again (or **Abort merge**). LitForge uses
  your own git sign-in (a credential helper or an SSH key) and never asks for passwords; tokens in URLs are hidden.
- **Compile** (Ctrl/Cmd+Enter) builds the PDF shown on the right. Errors and warnings appear under the editor with
  their line; click one to jump to it. Install the engine once in Settings → Optional tools (Tectonic); the first
  compile downloads the LaTeX packages it needs (about two minutes), later compiles take about a second and work
  offline.
- **Completion.** A small list appears under the cursor while you type: LaTeX and `xepersian` commands after `\`
  (with your own `\newcommand`s), the document's labels inside `\ref{`, environments inside `\begin{` (the
  matching `\end` is added) and keys inside `\cite{`: the ones already in the document first, then the project's
  papers. Enter or Tab picks, Esc closes; the last row of the `\cite{` list opens the full search.
- **Cite** searches the project's papers, or quotes in them under **Evidence**. The key is
  inserted at the cursor (or added to the `\cite{...}` you are in) and the paper's entry is written to
  `references.bib`. Keys look like `lee2021brain` and never change once made. Entries you write yourself in
  `references.bib` are kept; LitForge rewrites only the block between its `% litforge:` markers. With the cursor
  in a `\cite{...}`, the palette offers **Show quote** for the passage the citation came from.
- **Assistant** opens beside the editor: ask about your text or the project's papers. Answers quote the papers
  (each quote is checked against the paper) and **Insert \\cite** adds a source at the cursor. With text selected you
  can ask for a change; it comes back as a suggestion to accept or reject, with the same checks as the AI actions.
  With nothing selected you can ask it to add something at the cursor ("a table with 3 rows and 5 columns", "an
  equation for …"); the LaTeX comes as a suggestion on its own lines, table cells as placeholders, never data.
  What is sent: your question, the selection (or a few lines around the cursor), the last messages and matching
  passages from the project's papers. The local model is used unless you assign a cloud model to `write_chat`.
- **AI** works on the selection only: **Fix this error** (click a compile error first), **Rewrite: clearer /
  shorter**, **Translate** to English or Persian, and **Suggest citations** from the project's evidence. The local
  model is used unless you assign a cloud model to the `write_fix`, `write_rewrite` or `write_cite` task; a cloud
  route asks before sending and says what is sent. The answer is shown as changes you accept or reject one by one,
  one changed sentence at a time with the removed and added words marked inside it (right to left for Persian);
  nothing changes before you click **Apply accepted**. Changes are flagged (and start as rejected) when they add a
  number that is not in your text or the evidence, a citation key that is unknown or not backed by a quote, drop
  LaTeX commands, math, `\cite`, `\ref` or `\label`, or add commands. Suggested citations are kept only when
  their quote is found word for word in the paper.
- **Suggest title / Suggest abstract** write them from the document's own text (this file and the files it
  includes), in its language, and replace what is in `\title{...}` or the `abstract` environment; without an
  abstract, a new one is proposed after `\maketitle`. Numbers that are not in the document are flagged. Tasks:
  `write_summary`.
- **Equation from image / Table from image** open a small panel at the cursor: paste a screenshot (Ctrl/Cmd+V)
  or choose a PNG, JPEG, GIF or WebP file (at most 8 MB). The local vision model (`qwen2.5vl:7b`, task
  `write_image`) writes the LaTeX, which is proposed for insertion at the cursor on its own lines; check every
  symbol and number against the image before applying. A cloud route asks before sending the image.

From the command line:

```bash
litforge write new "My review" -p my-review --template persian
litforge write list -p my-review
litforge write compile DOCUMENT_ID
litforge export -p my-review --format bibtex > references.bib
```

**Compile safety.** Shell escape is off and a document cannot write outside its folder. Tectonic's own
"untrusted" mode does not stop a document from *reading* other files, so on macOS compiling runs inside the
system sandbox (`sandbox-exec`) and on Linux inside `bwrap` when it is installed; there the document can read only
its own folder, the engine and its package cache, and only Tectonic may use the network (to fetch LaTeX packages). `latexmkrc` files are refused, since latexmk would run them as code. On Windows, and on Linux without `bwrap`, LitForge refuses
documents that name files outside their folder, but a determined document could still read other files: the
Write view says "partial sandbox" then, so compile only documents you trust.

## Where things are stored

All data lives in `./data` (git-ignored). Use `--data-dir PATH` or `LITFORGE_HOME=PATH` to move it.

```text
data/
├── library/              shared by all projects
│   ├── papers.sqlite     metadata, sections, search index
│   ├── pdfs/             original PDFs, named by SHA256
│   └── parsed/           per-PDF output: canonical.json, paper.md, assets/
└── projects/<name>/      project settings
```

The **Library** view labels papers **Not in a project** and offers an **Unused only** filter.
Use **Remove unused papers…**, or **Remove from library** in an unused paper's drawer, to preview
paper/PDF counts and disk space before confirming irreversible deletion. The CLI equivalent is
`litforge library remove-unused` (`--paper ID` is repeatable; `--yes` skips confirmation).
Deleting a project still keeps its papers. Cleanup refuses papers used by any project or whose evidence
is cited by an existing analysis, including detached runs, model calls, snapshots and review history.
Unreferenced evidence spans are deleted; referenced spans retain their immutable audit protection.
Only shared-library PDFs and parsed folders are collected after the database commits; user originals
and exports stay intact. File failures are reported, and space estimates exclude SQLite's database file.

## How a PDF is processed

1. **Import:** the file is hashed; identical files are stored once. Corrupt or encrypted files
   are kept but marked `quarantined`.
2. **Parse:** PyMuPDF extracts text with page numbers and positions, restores two-column reading
   order, removes running headers, and detects sections, captions, figures, tables and display
   equations (saved as image crops). A quality check marks papers that need a stronger parser
   (`needs_docling`) or OCR (`needs_ocr`).
3. **Index:** text is split into section-aware chunks and indexed with SQLite FTS5.

[MinerU](https://github.com/opendatalab/MinerU) is the accurate local parser for equations,
superscripts and tables. Install the pinned 4.0.12 release with `litforge tools install mineru`
(also available as `litforge parse install-mineru`, or in Settings → Optional tools). LitForge previews the size and asks before downloading;
`--yes` confirms from the command line. The isolated environment lives in `<data>/tools/mineru`,
with about 2 GB of models under `<data>/models/mineru`, downloaded on first use; torch and other
dependencies need additional disk space.

Allow roughly **20 seconds per page on GPU**, longer on CPU (about 46 seconds per page in our
sample). MinerU needs substantial memory. LitForge stops its own managed llama-server when its
slots report it is idle; a GPU memory failure triggers one CPU retry. The model server loads again
on the next model call.

The default `parsing.complex_fallback: auto` picks the structural parser like this:

| Situation | Parser |
|---|---|
| Nemotron Parse is set up, an NVIDIA GPU has 3 GB free, the document is not Persian | Nemotron Parse (also reads scans without a text layer) |
| No GPU, or a Persian document | MinerU, if installed |
| Neither | Docling (`uv sync --extra docling`), if installed; otherwise the fast text is kept |

If one fails (Nemotron also gives up on a document it reads as Persian), the next is tried. You can
choose `nemotron`, `mineru`, `docling` or `none` in config or in Settings → Optional tools → **Which
engine reads what**; `nemotron` chosen by hand also runs without a GPU, at about two minutes per page.

[Nemotron Parse](https://huggingface.co/nvidia/Nemotron-3.5-Parse) (NVIDIA, 0.6B parameters) reads a page
image into headings, text, LaTeX equations, tables (merged cells kept) and figure regions. On an RTX 5070
it took about 5 s and 2 GB of GPU memory per page, and read tables better than MinerU and the vision
model in our comparison; it mixes up Persian text with Latin words and numbers, so Persian pages and
documents never go to it automatically. Setting up Math: high on a computer with an NVIDIA GPU sets it
up too, or use Settings → Optional tools or `litforge tools install nemotron`: two small packages
(`uv sync --extra formulas-gpu --extra nemotron --inexact`, sharing Pix2Text's CUDA PyTorch) and the
model from one pinned revision (about 2.5 GB under `<data>/models/nemotron-parse`). Its model code
runs locally from that download; pages never leave the computer.
`litforge parse --parser mineru --paper ID` (or `--parser nemotron`) re-reads a selected paper; repeat
`--paper` for more.
The paper drawer's **Re-read accurately (MinerU)** button runs in the background with a time
estimate ("about N min", based on successful MinerU runs in this library, initially 20 seconds per page)
and status. Successful forced MinerU runs become current and re-index the paper;
failed attempts preserve the previous run and earlier evidence stays available.
`--parser pymupdf` selects the fast parser alone, and the legacy `--no-docling` disables all
automatic structural fallback.

**Privacy:** PDFs stay on this computer. LitForge invokes only local parsing, never MinerU's
remote service, strips API keys from the parsing process, and disables interactive telemetry
prompts. Installation and first-use model downloads require internet access.
Installation allows 45 minutes in total (`parsing.mineru.install_timeout_s: 2700`); failure, timeout or
interruption removes the tool directory created by that attempt and preserves existing installations.

## Better math (equations and tables)

The text layer of a PDF keeps prose well but scatters display equations into single characters, and
in scanned papers often misses them. Pages that show it (an equation, a table, an equation number,
fragment lines) can be rewritten at two levels, before exporting (**Math** menu in the web UI) or with
`litforge enhance --level medium|high`:

| Level | How | Speed | Needs |
|---|---|---|---|
| medium | [Pix2Text](https://github.com/breezedeus/Pix2Text) finds every formula (inline and displayed) and reads it as LaTeX; the text stays as extracted | ~1.5 s/page on a GPU, ~5 s on a CPU | ~2 GB; Settings → Optional tools or `litforge tools install pix2text`; otherwise `uv sync --extra formulas-gpu --inexact` (NVIDIA) or `uv sync --extra formulas --inexact` (CPU) |
| high | whole pages are rewritten: formulas, tables and text, also in scanned papers; a transcription that does not match the page's text is rejected. On an NVIDIA GPU, Nemotron Parse rewrites pages that are not Persian; the local vision model (Ollama, `qwen2.5vl:7b`) rewrites Persian pages, and every page on a computer without a GPU | Nemotron ~5 s/page; the vision model ~12 s/page on a GPU, about a minute without | `ollama pull qwen2.5vl:7b` (8 GB of memory); the page offers to install Ollama and the model, and Nemotron Parse on an NVIDIA GPU |

`vision.engine` (Settings → Optional tools → **Which engine reads what**) changes the high level:
`auto` as above, `nemotron` for every page, or `vision_model` for every page. With Nemotron set up but
no vision model, `auto` still rewrites the English pages and Persian pages keep their extracted text.

Results are kept per page, so a later export or a higher level only processes what is new. Each
note's front matter says how its equations were produced (`math: raw | medium | high`), so a
language model reading the notes knows how far to trust them: a sensible workflow is to screen
many papers with plain exports and rewrite only the ones you read closely.

## Optional tools

The base installer keeps large tools out of the default setup. It asks separately about MinerU and
Pix2Text when run in an interactive terminal; answers default to No. Set
`LITFORGE_EXTRAS="mineru,pix2text"` to install either or both without prompts, or
`LITFORGE_NO_PROMPT=1` to suppress prompts. Failed optional installs do not undo the base install.
Pix2Text installation times out after 45 minutes by default; set
`LITFORGE_PIX2TEXT_INSTALL_TIMEOUT_S` to change the limit.
The final installer hint and Settings → Optional tools list the same options.

| Tool | Approximate size | What it improves | Install |
|---|---:|---|---|
| MinerU | ~4 GB | More accurate local PDF reading of equations and tables | Settings → Optional tools; `litforge tools install mineru` (alias: `litforge parse install-mineru`); installer prompt or `LITFORGE_EXTRAS=mineru` |
| Nemotron Parse (NVIDIA GPU) | ~2.5 GB model, plus Pix2Text's CUDA PyTorch | Fast, accurate reading of English pages and tables, for Math: high and broken PDFs | Set up with Math: high on an NVIDIA GPU; Settings → Optional tools; `litforge tools install nemotron` |
| Pix2Text (Math: medium) | ~2 GB | Reads detected formulas as LaTeX in exported notes | Settings → Optional tools; `litforge tools install pix2text`; installer prompt or `LITFORGE_EXTRAS=pix2text`. Chooses the NVIDIA extra when a GPU is detected. |
| Tectonic (Write) | ~22 MB, plus ~50 MB of LaTeX packages on the first compile | Compiles Write documents to PDF, English and Persian | Settings → Optional tools; `litforge tools install tectonic`. A system TeX Live/MacTeX (`latexmk` + `xelatex`) also works. |
| Image/vision model (Math: high) | Depends on selected model | Rewrites whole pages, including formulas, tables and scanned text | Settings → Models (or the legacy setup action); the installer does not download models. `litforge tools` shows the resolved model status. |

On 31 pages of 12 arXiv papers, scored against the papers' own LaTeX, both levels read about 95% of
each displayed equation correctly (character similarity), Pix2Text finding a few more and the vision
model getting more exactly right; Nougat and UniMERNet did no better and were slower.

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

Benchmark the parser on a set of open-access arXiv papers:

```bash
uv run python scripts/fetch_benchmark.py     # downloads PDFs to data/benchmark/pdfs
uv run python scripts/benchmark_parsers.py   # writes data/benchmark/parser_benchmark.csv
```

To keep the virtual environment outside the repository, set `UV_PROJECT_ENVIRONMENT`
(for example `export UV_PROJECT_ENVIRONMENT=$HOME/envs/litforge`) before running `uv`.

## Status and limitations

Implemented: import, parsing, search, Markdown export, paper discovery (OpenAlex,
Crossref, arXiv, citation chaining, reference-list import) and title/abstract screening with a
local model. Planned: full-text screening and deep analysis of selected papers.

Known limitations of the fast parser:

- Tables drawn without vertical rules get a caption and crop but no cell grid (their text is
  still searchable).
- Without [better math](#better-math-equations-and-tables), equation text is approximate; the saved
  image crop is the reliable version.
- Some figures cannot be paired with their caption and get no crop.

Design documents (in Persian and English) are in this folder.

## License note

PyMuPDF is AGPL-licensed. Review this before distributing LitForge or derived work.

## Evidence answers and study matrices (CLI)

`litforge ask "How were neural mass parameters inferred?"` retrieves evidence, generates at
most eight cited sentences, then audits each cited sentence. Use `--no-audit` to omit the
second pass, `--audit-profile NAME` for a different auditor, or `--evidence-only` for the
original retrieval-only cards. `--json` includes sentence flags, audit verdicts and provenance.
All model calls use the configured gateway. Source text is treated as untrusted data;
unknown citations are removed and uncited facts become `unsupported`. Numeric checks compare
normalised numbers (including decimal, scientific and thousands notation) with the cited
text actually sent to the model. These checks validate citations and numbers, not semantic truth.

A question written in Persian finds evidence in English papers: the local model first turns it into English
search terms (the `query_translate` task, local in every mode, cached per question), which are searched together
with the words as written. The answer is written in Persian and its quotes stay in the paper's English. The same
step runs for `litforge search`, Writing Studio citations and the assistant. If the model is not available, the
words are searched as written. On the sbi10 benchmark this raised Persian Recall@10 from 22% to 66% (English:
67%).

Questions get the strongest model available: unless you assigned `ask` or `write_chat` yourself, they go to the
cloud model when the cloud is enabled (the strong one first), otherwise to the largest text model installed in
Ollama that fits this computer's memory (no download; embedding and image models are skipped), and to the
configured local model when nothing larger is installed. Screening, extraction and translation stay on the
faster local model. On the benchmark, `qwen3:8b` answered 16 of 20 Persian questions in good Persian where
`qwen3:4b` answered 11 and often repeated the question (such an answer is replaced by a note).

Ask also shows verified exact supporting quotes under each sentence (up to three, 400 characters each).
Click a quote's paper/page chip to open its evidence with the original passage highlighted. Quotes are
checked against the evidence actually sent: the words must match in order, while case, spacing, punctuation,
PDF ligatures (ﬁ) and line-break hyphenation may differ; hyphens inside words, signs, comparison symbols and
digits must match exactly.
`quote_not_in_evidence` ("Quote not in evidence") means a proposed quote was dropped;
`unquoted_fact` ("No exact quote") means a cited fact has no verified quote. Both enter the needs-review
queue even if an audit supports the sentence. "Not every citation quoted" is informational: the sentence has a
verified quote, but another evidence span it cites has none. Research briefs include verified quotes below each
sentence.

```bash
litforge ask "How were parameters inferred?" --profile local --preview
litforge ask "How were parameters inferred?" --sensitive --json
litforge audit ANSWER_RUN_ID --profile local
litforge review --json
litforge review accept SENTENCE_OR_CELL_ID --reason "checked the source"
litforge review reject SENTENCE_OR_CELL_ID --reason "incorrect extraction"
litforge review edit CELL_ID '3.25' --reason "corrected from the table"
litforge brief ANSWER_RUN_ID --matrix studies -o brief.md

litforge matrix create studies --question "How were neural models evaluated?" --columns configs/matrix.example.yaml
litforge matrix extract studies --preview
litforge matrix extract studies
litforge matrix add-column studies method --label Method --instruction "Describe the inference method" --type text
litforge matrix extract studies  # only new/changed cells; --force re-extracts
litforge matrix show studies
litforge matrix export studies --format csv -o studies.csv
litforge matrix list
```

Commands use the active project, or `-p NAME`. Matrix names are unique within a project;
IDs can also identify matrices. `matrix extract --paper ID` can be repeated to select rows.
Cells distinguish `reported`, `not_reported`, `not_found`, `not_applicable`, and `error`.
Markdown exports include evidence footnotes and unit/retrieval warnings; CSV includes a status
and evidence column for every field; JSON includes full provenance. Extraction run results
retain historical model values. Human cell overrides persist through cache reuse and forced
extraction; similar cells are never changed automatically. Fresh model extractions reopen previous
accept/reject decisions unless a human value override is present. Human review history is append-only.
Number/boolean/text edits must match the column type, and categorical edits must match an option.
When multiple artifacts have current parses for a paper, extraction uses the newest parse.
Cached errors require `--force` to retry, like other unchanged cells.

`ask`, `audit` and `matrix extract` accept `--profile NAME`, `--approve`, and `--preview`.
Preview never dispatches a model call and exits 0. A route requiring cloud approval prints the
plan and exits 3 unless `--approve` is supplied. Plans show routes, evidence counts, estimated
tokens, maximum costs and blocked reasons. For automatic audit, the ask preview conservatively
includes up to eight audit calls; actual calls depend on the generated sentences. Matrix runs
stop dispatching when the run budget is exhausted and mark remaining cells `error: budget`.
Use `--sensitive` on ask to retain sensitive-content approval requirements during later audits.

The retrieval benchmark YAML can also contain `matrix:` targets with `paper`, `column`,
`status`, and optional `value`. `litforge eval matrix BENCHMARK --matrix studies` reports status
accuracy, value accuracy (numbers within 1% relative; text case-insensitive exact/contained),
and whether cited spans belong to their row's paper. `litforge eval answers BENCHMARK --profile
local` reports citation precision against relevant targets (invalid IDs count as incorrect citations),
unsupported-sentence rate, and invalid citation count. Both accept `--json` and store analysis runs. Briefs refuse any factual sentence
without evidence or an explicit unsupported/interpretation label.


### Evidence Lens in the web UI

Select a project, then open **Ask** to ask a question about its library evidence. Sentence rows show
kind, validation flags and audit verdicts; citation chips open the quote, paper, section and PDF page
in the drawer. **Run audit** checks the stored answer with the chosen profile. The **Audit** filter
shows weak or single-source sentences; **Answer · Evidence · Needs review** switches between the
answer, source cards and the project's open reviews. Accept, reject or edit a sentence or cell in the drawer.

Open **Matrix** to create a comparison with named, typed columns, extract values, inspect cells and
export Markdown, CSV or JSON. Add columns to an existing matrix and extract again to fill only new
or changed cells. Unit and retrieval warnings appear above the table. Number and boolean edits use
numbers and `true`/`false`; categorical edits use an exact option. A blank profile uses the configured
local default. Every remote action shows provider, model, evidence count, tokens and maximum cost
before confirmation. Press **?** for the guide and **Esc** to close the drawer or a dialog.

## Claims, full-text screening and review queue

```bash
litforge claims extract -p my-review --profile local --preview
litforge claims extract -p my-review --paper PAPER_ID  # repeat --paper to select papers
litforge claims cluster -p my-review
litforge claims relate -p my-review --max-pairs 200
litforge claims show -p my-review --cluster CLUSTER_ID --json
litforge claims landscape -p my-review
litforge claims review accept CLAIM_ID
litforge claims review reject CLAIM_ID --reason "source disagrees"
litforge claims review edit CLAIM_ID --text "Corrected finding" --reason "checked source"
litforge screen --fulltext -p my-review --top 100
litforge queue -p my-review -n 20 --json
litforge brief ANSWER_RUN_ID --claims -o brief.md
litforge eval screening benchmark.yaml -p my-review --json
```

Claim extraction uses result/discussion/conclusion/abstract sections, with project-question FTS
retrieval as fallback, then the first chunks of the paper (body before front matter, up to eight).
Papers without usable text are counted as `no_text` in the extraction report and CLI output.
Each paper yields at most eight cited claims; unknown IDs are removed,
uncited claims dropped, identical claims within a paper deduplicated. Cache keys include the current
parse, provider, model and prompt. `--force` re-extracts while preserving human reviews and edits.
Clustering uses TF-IDF with an outcome-match boost and links only claims from different papers.
Relations require model assessment; polarity rules only prioritise candidate contradictions.
All possible causes are labelled hypotheses; missing citations additionally flag an unsupported hypothesis.
The landscape displays distinct-paper evidence strength separately from citation popularity.

S2 retrieves up to three chunks per criterion and makes one call per parsed paper. A violated exclusion
must cite an exact quote inside its own transmitted evidence span (whitespace may vary); otherwise its
exclude suggestion becomes uncertain. Unparsed papers remain S1 and appear in `needs_pdf`.
S1 suggestions, S2 decisions and human ✓/✗ changes append reasons and criteria versions to immutable
screening history. S1 also requires a verified exclusion quote and keeps the missing-abstract rule. Its
`screen-v2` prompt adds an untrusted-text instruction, so earlier cached S1 results are screened again.
Marks remain human decisions.
The queue only ranks unmarked papers; it never changes marks or model decisions.

In the UI, **Claims** lists clusters, claims, relations, hypotheses and reusable evidence cards, with
accept/reject controls. Results show **S2** badges, nearby-mark relevance predictions and a **Review queue**
sort with reason tooltips. Paper details include S2 evidence and screening history. Claims and full-text
screening offer model-profile selectors and the same remote-preview confirmation as Ask/Matrix.
CLI model commands accept `--profile`, `--approve` and `--preview`; preview makes no model calls.
Budget exhaustion preserves completed work and reports `budget_stopped`.

Benchmarks may contain only screening labels:

```yaml
name: screening-check
screening:
  - paper: PAPER_ID  # also accepts a DOI or arXiv ID
    relevant: true
```

Evaluation reads stored S1/S2 results, reports each stage's false-negative, exclusion and uncertain rates
and shortlist recall, and saves an analysis run. Exit code 1 means a false-negative rate exceeds
`screening.max_false_negative_rate` (default `0.05`). Missing stage results count as uncertain and stay
on the shortlist; the report also shows how many papers were screened.

Implementation choices where the design leaves a formula or representation open:

- Human marks use stage S3; clearing a mark records uncertain with reason “Mark cleared”.
- With no model score, uncertainty is 1. Coverage is the mean of the missing-parse and missing-abstract
  indicators. Boundary similarity averages the five closest marks on each side; kNN uses the five closest
  overall with similarity weights, returning no prediction when all similarities are zero. Exact ties
  predict 0.5 and do not constitute disagreement. Queue ties use paper ID.
- Distinct papers stand in for independence. Similarity alone never proves support or replication: counts
  use explicit relations, with the earliest dated paper as origin (unknown years sort last, then paper ID).
  Contradicting relations attribute the challenge to the later paper; replication counts distinct later
  papers with a replication relation. Contradicting papers are removed from the supporting count.
- S2 criterion evidence is `{evidence_id, quote}`; IDs outside the transmitted packet are removed and
  counted as `invalid_citations`. S1 verifies quotes against title and abstract; S2 requires an exact
  quote in the cited span (allowing whitespace differences). Editing a claim invalidates its model relations until
  reassessed. Project deletion preserves append-only screening history and human review records.

## Figure intelligence and research gaps

In the paper drawer, analyze an individual table, figure or equation crop; only that crop is sent
to the vision-capable model: by default the local vision model (`vision.model`, `qwen2.5vl:7b`),
or a profile you pick. Remote calls use the normal preview, approval and budget checks.
Transcribed cells, digitized chart points and equations are image-derived evidence, not text-reported
values; matrix and Ask validation flags numbers supported only by image evidence. The glossary groups
equation symbols by meaning.

Re-parsing existing papers improves equation crops, especially centred equations in single-column papers;
the updated PyMuPDF parser version invalidates older cached parses. Crop transcription preserves only visible
content, uses `\cdots` at cut edges, and shows a `cut_off_crop` fidelity flag in the UI and CLI.
Default output limits are 4,096 tokens for tables/charts, 1,024 for equations, 2,048 for claim extraction,
and 8,192 for page rewrites (reduced to fit the profile's context). Task `max_output_tokens` overrides take
priority over profile limits and these defaults. Charts request at most 25 points per series.

```bash
litforge asset analyze ASSET_ID --mode table --preview
litforge asset analyze ASSET_ID --mode table --approve
litforge glossary -p my-review
litforge gaps -p my-review
litforge gaps design GAP_ID --preview
litforge gaps design GAP_ID
litforge gaps review CARD_ID accept
litforge gaps review CARD_ID reject --reason "baseline is not appropriate"
litforge gaps export CARD_ID -o proposal.md
```

The **Gaps** view lists deterministic candidates from claims and matrices and supports proposal design,
review and export. Generated cards are titled **Research proposal — not a literature finding**; accept
a card before it can be exported. Review decisions are retained in append-only history, including an
optional reason. Exports use evidence footnotes and a short, escaped evidence appendix. A gap that the
current evidence no longer shows (say a single-source claim gained support) disappears from the list
and its card can no longer be exported.

A matrix column marked `--parameter` is flagged when no sensitivity-analysis column covers it. Mark the
covering column with `--sensitivity-for PARAMETER_KEY` (repeatable), or name the parameter in its label
or instruction:

```bash
litforge matrix add-column studies rate --label "Firing rate" --instruction "Report the rate" --type number --parameter
litforge matrix add-column studies rate-sens --label "Rate varied?" --instruction "Was the rate varied?" \
  --type boolean --sensitivity-for rate
```

## Living reviews and reproducibility radar

```bash
litforge snapshot -p my-review --label "Submitted draft"
litforge snapshots -p my-review
litforge delta OLD_SNAPSHOT NEW_SNAPSHOT --md changes.md
litforge refresh -p my-review --notify
# Optional: --no-screen --no-extract --no-claims; --approve for configured cloud tasks
litforge watch install -p my-review --every 7d
litforge watch remove -p my-review
litforge repro -p my-review --paper PAPER_ID --md reproduction.md
# Repro also supports --profile, --preview and --approve, like matrix extraction.
```

**Claims → Snapshots** lists immutable snapshots, provides **Take snapshot** and **Refresh**, and shows
latest delta signals and the report. Refresh runs in the background in the UI. It replays stored search
queries (including keyword queries saved by similar-paper discovery), bypasses the HTTP cache, screens
only papers added since the latest snapshot with the current project criteria, and runs S2 when text
exists. Existing matrices use their cache keys to extract new or changed cells. Claims are extracted
only for new papers, reclustered, and related only in pairs involving new claims. Refresh uses the
configured task routes. CLI `--approve` authorizes cloud use for that refresh; the UI asks for cloud
confirmation when remote tasks are assigned. Unattended work never approves a cloud call. Delta Markdown is written
as `delta-RUN_ID.md` in the project's export directory. Partial discovery or budget-limited extraction
is recorded as `partial`; completed work is kept. Answers are compared as stored, not regenerated.

Signals are limited to a new included paper at or above the priority 75th percentile, an existing
result's strength changing by at least one, a new contradiction, a reported matrix value changing,
or changed answer evidence. New-paper counts alone never trigger a signal. `--notify` calls the OS
notifier only when signals exist; notification failures do not change the refresh command's exit code.

Scheduling is opt-in: only `watch install` writes a launchd plist or systemd user service/timer.
It prints the explicit activation command; LitForge does not call `launchctl` or `systemctl`.
`watch remove` removes those files and prints the deactivation command if needed. On Windows, install
and remove print Task Scheduler commands without executing them. The schedule preserves the absolute
data directory, explicit config path and project; it runs `litforge refresh -p NAME`.

The paper drawer's **Repro** tab shows a 12-item reproducibility checklist, recorded code/data links,
and a reproduction brief. **Extract reproducibility** uses the built-in `reproducibility` matrix and
the existing model preview, validation, cache and review machinery. The matrix is also available in
**Matrix** for inspection and review. Link extraction examines every chunk of current full text,
recognises repository hosts and known data DOI prefixes, and stores the source evidence ID. Zenodo,
Figshare and OSF links are listed as "other", since those sites also hold papers and software. Links
are shown as inert text and are never fetched or executed. Brief observations cite evidence; missing
information is labelled unsupported, suggestions are labelled proposal, and estimates are labelled
interpretation. Compute estimates use reported hardware and a reported duration only.

Conservative choices where Phase 3 leaves details open:

- Priority uses each paper's highest stored priority in this project; the percentile is nearest-rank
  over known priorities in the after snapshot. Unknown priorities cannot trigger a signal. Human marks
  take precedence over S2, then S1, when determining inclusion.
- A duplicate query is replayed once with the union of its saved sources and the largest saved limit
  (25 for similar-search keyword hits without a stored limit). Imports and citation-ranking topics
  are not search queries and are not replayed as searches.
- Cluster overlap through shared claim IDs matches growing, merging and splitting clusters, despite
  changing cluster IDs. Newly isolated claims are listed as single-source, not as stronger existing
  results. Gaining or losing a reported cell value counts as a value change; evidence-only cell changes
  appear in the delta but do not create a matrix-value signal.
- Snapshots and watch history survive project deletion. Repro links from older parses remain stored
  as provenance, but cards show links and matrix observations from current parses only. A paper-only
  card uses a unique project reproducibility matrix; multiple project matrices require `-p` to select
  reviewed observations. With no extraction or ambiguous context, checklist items remain unclear.
- Schedule installation writes files and prints activation instructions; activation/deactivation is
  explicit. Compute estimates require both hardware and a duration; otherwise they say “not reported”.

## Update notices

**Settings → Updates** checks whether the imported LitForge installation is behind `origin/main`.
Automatic checks are enabled by default and run at most once every 24 hours while the UI is open.
The check contacts GitHub using `git fetch` (or the checkout's configured `origin` remote).
It sends no library data: no papers, PDFs, searches, settings or model credentials are uploaded.
Only Git metadata is fetched; your files and branches are left as they are.
Results, including connection failures, are cached in `<data>/state/update-check.json`.

Turn off **Automatically check for updates daily** to disable automatic checks. This saves
`"update_check": false` in `<data>/settings.json`, alongside other UI preferences.
Setting `LITFORGE_NO_UPDATE_CHECK=1` also disables automatic checks, regardless of the checkbox.
When disabled, no notice or settings badge appears until you explicitly click **Check now**.
That button always checks, even when automatic checks are disabled.

An available update adds a small dot to the sidebar's Settings button. Copy the displayed command,
close LitForge, then run it in a terminal. **LitForge never runs the updater or `git pull` for you.**
The command points at this installation's folder (`LITFORGE_DIR=...`), so a copy installed in a
custom folder updates itself, not `~/LitForge`. On a branch other than `main` it shows
`git switch main && git pull --ff-only` instead. Local changes are kept; if they conflict, the update
stops without changing anything. Diverged histories require manual reconciliation before updating.
A copy installed without git (from the ZIP) cannot update itself, and the installer leaves it
unchanged: download the new version and replace the program files, keeping your data folder.

`litforge update` forces a check and prints the result and update command. For scripts,
`litforge update --check` exits **0** when up to date (including a checkout that is only ahead),
**10** when an update is available, and **1** for errors, non-Git installs or diverged histories.
CLI checks also bypass disabled automatic checks. Git commands have a combined 10-second timeout.

### PRISMA review report

Run `litforge prisma -p NAME` to write a standalone PRISMA 2020 diagram to the
project's export folder as `prisma.svg`; the command prints its path. Use
`litforge prisma -p NAME -o report.svg` for a custom path, or `--json` for counts
without writing a file (`--json -o report.svg` does both).

In a project's recent searches view, open **Review report** to see the counts and
diagram. **Save** writes the SVG into the export folder and shows the saved path;
**Open folder** opens that folder. Human screening marks override model decisions,
uncertain decisions stay separate from included papers, and confirmed versions
count once, with other versions counted as duplicates. Papers awaiting screening,
retrieval, or full-text assessment remain visible in the report.

### Export references with screening decisions

Use the header **RIS** or **BibTeX** buttons to save included primary papers with their
final screening decisions. The saved path appears with **Open export folder**.
Human marks take precedence over model decisions; uncertain papers are not included by default.

```bash
litforge export-refs -p NAME --format ris
litforge export-refs -p NAME --format bib --status included --status uncertain -o references.bib --json
```

The default output is `<export folder>/references.ris` or `references.bib`. Repeat
`--status` to select `included`, `excluded`, `uncertain`, or `unscreened`. `--json`
prints `{"path": ..., "count": ...}` after saving. RIS notes and BibTeX notes and
keywords carry the status, stage, source, and decision reason. BibTeX citation keys
match the Writing Studio.

### Search strings from screening criteria

Save a research question and inclusion/exclusion criteria in **AI screening**, then use
**Search strings** to select databases and **Build**. The model extracts concepts only;
LitForge formats PubMed, Scopus, Web of Science, arXiv and OpenAlex queries itself.
Each readonly query has **Copy**; if clipboard access is unavailable, select and copy
it manually. OpenAlex's **Search with it** starts the existing search flow with up to
six plain queries combining each concept's first two terms.

```bash
litforge strategy -p my-review --db pubmed --db scopus --preview
litforge strategy -p my-review --db pubmed --db scopus --approve --json
```

The default databases are PubMed, Scopus and OpenAlex. The `search_strategy` task
uses the local model unless assigned another profile in settings. Preview shows the
route and approval requirements without calling the model. Only the question and
include/exclude criteria are sent, never paper text. Results are cached by criteria
hash and database selection; reopening or rebuilding unchanged criteria costs no
model call. Changing criteria or databases builds a fresh strategy. Warnings report
trimmed concepts/synonyms and sanitised or dropped terms. Review the strings before
using them in an external database.
