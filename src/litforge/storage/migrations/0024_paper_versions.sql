ALTER TABLE artifacts ADD COLUMN version TEXT NOT NULL DEFAULT 'unknown'
    CHECK (version IN ('published', 'accepted', 'submitted', 'unknown'));
ALTER TABLE artifacts ADD COLUMN version_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (version_source IN ('openalex', 'unpaywall', 'host', 'pdf_text', 'user', 'unknown'));
ALTER TABLE artifacts ADD COLUMN source_url TEXT;

CREATE TABLE paper_versions (
    group_id   TEXT NOT NULL,
    paper_id   TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('published', 'accepted', 'preprint', 'revision', 'unknown')),
    evidence   TEXT NOT NULL,
    confirmed  INTEGER NOT NULL DEFAULT 1 CHECK (confirmed IN (0, 1)),
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    rejected   INTEGER NOT NULL DEFAULT 0 CHECK (rejected IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (group_id, paper_id)
);
CREATE INDEX idx_paper_versions_paper ON paper_versions(paper_id);
CREATE INDEX idx_paper_versions_group ON paper_versions(group_id);
CREATE UNIQUE INDEX idx_paper_versions_primary ON paper_versions(group_id) WHERE is_primary = 1;
