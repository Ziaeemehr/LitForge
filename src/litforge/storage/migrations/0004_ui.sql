-- 0004: per-project relevance marks and a configurable export folder (web UI).

CREATE TABLE paper_marks (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id   TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    mark       TEXT NOT NULL,                     -- relevant | irrelevant
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, paper_id)
);

ALTER TABLE projects ADD COLUMN export_dir TEXT;   -- NULL = data/projects/<name>/exports
