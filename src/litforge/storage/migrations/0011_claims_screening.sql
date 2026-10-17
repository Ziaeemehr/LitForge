CREATE TABLE claim_clusters (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, label TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE claims (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, paper_id TEXT NOT NULL REFERENCES papers(id),
    text TEXT NOT NULL, canonical_text TEXT NOT NULL, canonical_hash TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('finding','method','hypothesis','limitation')),
    polarity TEXT NOT NULL CHECK(polarity IN ('positive','negative','null','mixed')),
    population TEXT, outcome TEXT, method TEXT,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','accepted','rejected','edited')),
    run_id TEXT NOT NULL REFERENCES analysis_runs(id), model TEXT NOT NULL, prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL, cluster_id TEXT REFERENCES claim_clusters(id) ON DELETE SET NULL,
    UNIQUE(project_id,paper_id,canonical_hash)
);
CREATE TABLE claim_evidence (
    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE, evidence_id TEXT NOT NULL REFERENCES evidence_spans(id),
    relation TEXT NOT NULL CHECK(relation IN ('supports','contradicts','qualifies')),
    strength TEXT NOT NULL CHECK(strength IN ('direct','indirect')), PRIMARY KEY(claim_id,evidence_id)
);
CREATE TABLE claim_extractions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, paper_id TEXT NOT NULL, cache_key TEXT NOT NULL, run_id TEXT NOT NULL,
    PRIMARY KEY(project_id,paper_id)
);
CREATE TABLE claim_relations (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    target_claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK(relation IN ('supports','contradicts','qualifies','replicates','unrelated')),
    explanation TEXT NOT NULL, causes_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL CHECK(origin IN ('model','human','rule')), run_id TEXT REFERENCES analysis_runs(id),
    status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL, UNIQUE(source_claim_id,target_claim_id)
);
CREATE TABLE claim_reviews (
    id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, action TEXT NOT NULL,
    old_json TEXT NOT NULL, new_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE fulltext_screening (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, paper_id TEXT NOT NULL REFERENCES papers(id),
    stage TEXT NOT NULL DEFAULT 'S2' CHECK(stage='S2'), decision TEXT NOT NULL, score REAL NOT NULL,
    result_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
    criteria_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    screened_at TEXT NOT NULL, PRIMARY KEY(project_id,paper_id,stage)
);
CREATE TABLE screening_history (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, paper_id TEXT NOT NULL, stage TEXT NOT NULL,
    decision TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('model','human')), model TEXT,
    prompt_version TEXT, criteria_hash TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER screening_history_no_update BEFORE UPDATE ON screening_history
BEGIN SELECT RAISE(ABORT, 'screening history is append-only'); END;
CREATE TRIGGER screening_history_no_delete BEFORE DELETE ON screening_history
BEGIN SELECT RAISE(ABORT, 'screening history is append-only'); END;
CREATE TRIGGER screening_history_no_reinsert BEFORE INSERT ON screening_history
WHEN EXISTS (SELECT 1 FROM screening_history WHERE id=NEW.id OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'screening history is append-only'); END;
CREATE INDEX idx_claims_project ON claims(project_id,cluster_id);
CREATE INDEX idx_screening_history_paper ON screening_history(project_id,paper_id);
