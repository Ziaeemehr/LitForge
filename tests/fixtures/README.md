`openrouter_models.json` records the relevant fields of OpenRouter's documented
`GET /api/v1/models` response example, retrieved 2026-10-18:
https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties

It is a fixed historical fixture, not a source of runtime prices. Tests never fetch it.

`models/hf-search.json` and `models/hf-tree.json` are reduced, anonymized schema fixtures based on the
public Hugging Face responses verified in the task (2026-10-18). Counts and checksums are placeholders;
unit download streams use real checksums computed from deterministic bytes.
`models/openrouter.json` extends the documented recorded catalogue with synthetic vision, free, invalid
and hostile-name entries for filtering and DOM-safety tests. Runtime IDs/prices are never sourced from fixtures.

`models/openrouter-recorded.json` retains the catalogue fields of two actual public
`https://openrouter.ai/api/v1/models` records retrieved 2026-10-18 through the web reader (outside tests).
Direct network access from the workspace and the web reader's HF API access were blocked, so the HF
fixtures above use the task's verified response shapes with explicitly synthetic checksums/counts.
