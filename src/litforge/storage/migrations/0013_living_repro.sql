-- Snapshots survive project deletion and cannot be replaced through either identity.
CREATE TABLE project_snapshots (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, content_hash TEXT NOT NULL, data_json TEXT NOT NULL
);
CREATE INDEX idx_snapshots_project ON project_snapshots(project_id);
CREATE TRIGGER project_snapshots_no_update BEFORE UPDATE ON project_snapshots
BEGIN SELECT RAISE(ABORT, 'project snapshots are immutable'); END;
CREATE TRIGGER project_snapshots_no_delete BEFORE DELETE ON project_snapshots
BEGIN SELECT RAISE(ABORT, 'project snapshots are immutable'); END;
CREATE TRIGGER project_snapshots_no_reinsert BEFORE INSERT ON project_snapshots
WHEN EXISTS (SELECT 1 FROM project_snapshots WHERE id=NEW.id OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'project snapshots are immutable'); END;

CREATE TABLE watch_runs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
    before_snapshot TEXT NOT NULL REFERENCES project_snapshots(id),
    after_snapshot TEXT REFERENCES project_snapshots(id),
    delta_json TEXT NOT NULL DEFAULT '{}', signals_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    error TEXT, report_path TEXT
);
CREATE INDEX idx_watch_project ON watch_runs(project_id);
CREATE UNIQUE INDEX idx_watch_running ON watch_runs(project_id) WHERE status='running';

CREATE TABLE repro_links (
    paper_id TEXT NOT NULL REFERENCES papers(id), url TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('code','data','other')),
    evidence_id TEXT NOT NULL REFERENCES evidence_spans(id),
    PRIMARY KEY(paper_id,url,evidence_id)
);
