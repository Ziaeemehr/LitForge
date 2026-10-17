CREATE TABLE model_calls (
    id                TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    task              TEXT NOT NULL,
    input_hash        TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    error             TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_model_calls_created ON model_calls(created_at);
CREATE INDEX idx_model_calls_task ON model_calls(task);
