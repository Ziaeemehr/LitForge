CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    task TEXT NOT NULL, input_hash TEXT NOT NULL, prompt_version TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
    started_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX idx_analysis_runs_project ON analysis_runs(project_id, task);

ALTER TABLE model_calls ADD COLUMN run_id TEXT;
ALTER TABLE model_calls ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_model_calls_run ON model_calls(run_id);

CREATE TABLE evidence_spans (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL, parse_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    parser TEXT NOT NULL, parser_version TEXT NOT NULL,
    kind TEXT NOT NULL, section TEXT, page INTEGER NOT NULL, page_start INTEGER NOT NULL, page_end INTEGER NOT NULL,
    bbox_json TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX idx_evidence_spans_paper ON evidence_spans(paper_id);
CREATE TRIGGER evidence_spans_no_update BEFORE UPDATE ON evidence_spans
BEGIN SELECT RAISE(ABORT, 'evidence spans are immutable'); END;
CREATE TRIGGER evidence_spans_no_delete BEFORE DELETE ON evidence_spans
BEGIN SELECT RAISE(ABORT, 'evidence spans are immutable'); END;

CREATE TABLE run_evidence (
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence_spans(id),
    rank INTEGER NOT NULL, score REAL, PRIMARY KEY (run_id, evidence_id)
);
