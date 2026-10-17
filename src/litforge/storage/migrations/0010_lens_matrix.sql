CREATE TABLE answer_sentences (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, text TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('reported_fact','interpretation','unsupported')),
    evidence_ids_json TEXT NOT NULL DEFAULT '[]', flags_json TEXT NOT NULL DEFAULT '[]',
    audit_verdict TEXT, audit_note TEXT NOT NULL DEFAULT '', audit_model TEXT,
    review_status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,
    UNIQUE(run_id,idx)
);
CREATE TABLE matrices (
    id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    name TEXT NOT NULL, question TEXT NOT NULL, columns_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project_id,name)
);
CREATE TABLE matrix_cells (
    id TEXT PRIMARY KEY, matrix_id TEXT NOT NULL REFERENCES matrices(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL, column_key TEXT NOT NULL, column_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reported','not_reported','not_found','not_applicable','error')),
    value_json TEXT NOT NULL DEFAULT 'null', unit TEXT,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]', flags_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL, provider TEXT NOT NULL, prompt_version TEXT NOT NULL, parse_run_id TEXT NOT NULL,
    cache_key TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    review_status TEXT NOT NULL DEFAULT 'open', human_value_json TEXT, updated_at TEXT NOT NULL,
    UNIQUE(matrix_id,paper_id,column_key)
);
CREATE TABLE human_reviews (
    id TEXT PRIMARY KEY, target_kind TEXT NOT NULL CHECK(target_kind IN ('sentence','cell')),
    target_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('accept','reject','edit')),
    old_json TEXT NOT NULL, new_json TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TRIGGER human_reviews_no_update BEFORE UPDATE ON human_reviews
BEGIN SELECT RAISE(ABORT, 'human reviews are append-only'); END;
CREATE TRIGGER human_reviews_no_delete BEFORE DELETE ON human_reviews
BEGIN SELECT RAISE(ABORT, 'human reviews are append-only'); END;
CREATE TRIGGER human_reviews_no_reinsert BEFORE INSERT ON human_reviews
WHEN EXISTS (SELECT 1 FROM human_reviews WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT, 'human reviews are append-only'); END;
CREATE INDEX idx_answer_sentences_run ON answer_sentences(run_id);
CREATE INDEX idx_matrix_cells_matrix ON matrix_cells(matrix_id);
CREATE INDEX idx_human_reviews_target ON human_reviews(target_kind,target_id);
