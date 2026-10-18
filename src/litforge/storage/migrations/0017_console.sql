CREATE TABLE console_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    request TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    results_json TEXT NOT NULL DEFAULT '[]',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT,                       -- sanitised reason of the last failed step
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_console_runs_project ON console_runs(project_id, created_at);
