ALTER TABLE evidence_spans ADD COLUMN from_image INTEGER NOT NULL DEFAULT 0 CHECK(from_image IN (0,1));

CREATE TABLE asset_analyses (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('table','chart','equation')),
    result_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    fidelity_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_asset_analyses_asset ON asset_analyses(asset_id,created_at);

CREATE TABLE gap_candidates (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    source_key TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    review_status TEXT NOT NULL DEFAULT 'open' CHECK(review_status IN ('open','accepted','rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id,kind,source_key)
);
CREATE INDEX idx_gap_candidates_project ON gap_candidates(project_id,kind);

CREATE TABLE gap_cards (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES gap_candidates(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'open' CHECK(review_status IN ('open','accepted','rejected')),
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
