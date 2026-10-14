-- 0001: core library, parsing, chunks and project membership.
-- Discovery, screening and analysis tables arrive in later migrations.
-- Deleting rows never deletes original PDFs on disk.

CREATE TABLE papers (
    id            TEXT PRIMARY KEY,               -- UUID
    title         TEXT,
    doi           TEXT,                           -- normalized lowercase, may be NULL
    doi_status    TEXT,                           -- unverified | verified | NULL
    year          INTEGER,
    authors_json  TEXT,
    abstract      TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',     -- raw extracted metadata + per-field source
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_papers_doi ON papers(doi);

CREATE TABLE artifacts (
    id            TEXT PRIMARY KEY,
    paper_id      TEXT REFERENCES papers(id) ON DELETE RESTRICT,  -- NULL when quarantined
    sha256        TEXT NOT NULL UNIQUE,
    pdf_path      TEXT NOT NULL,                  -- relative to data root
    size_bytes    INTEGER NOT NULL,
    page_count    INTEGER,
    version_label TEXT,
    status        TEXT NOT NULL,                  -- ok | quarantined
    error         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_artifacts_paper ON artifacts(paper_id);

CREATE TABLE artifact_aliases (
    artifact_id   TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    original_path TEXT NOT NULL,
    filename      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, original_path)
);

CREATE TABLE duplicate_candidates (
    paper_id_a  TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    paper_id_b  TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL,                    -- doi | title
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | same_work | distinct
    created_at  TEXT NOT NULL,
    PRIMARY KEY (paper_id_a, paper_id_b, reason)
);

CREATE TABLE parse_runs (
    id             TEXT PRIMARY KEY,
    artifact_id    TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    parser         TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    config_hash    TEXT NOT NULL,
    status         TEXT NOT NULL,                 -- ok | needs_docling | needs_ocr | needs_review | failed
    route          TEXT,
    route_reasons  TEXT NOT NULL DEFAULT '[]',
    quality_json   TEXT NOT NULL DEFAULT '{}',
    output_dir     TEXT,                          -- relative to data root
    is_current     INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    elapsed_ms     REAL,
    created_at     TEXT NOT NULL,
    UNIQUE (artifact_id, parser, parser_version, config_hash)
);
CREATE INDEX idx_parse_runs_artifact ON parse_runs(artifact_id);

CREATE TABLE sections (
    id           TEXT PRIMARY KEY,
    parse_run_id TEXT NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    parent_id    TEXT REFERENCES sections(id) ON DELETE CASCADE,
    heading      TEXT,
    kind         TEXT NOT NULL,                   -- front | abstract | body | references | appendix
    level        INTEGER NOT NULL,
    order_idx    INTEGER NOT NULL,
    page_start   INTEGER,
    page_end     INTEGER
);
CREATE INDEX idx_sections_run ON sections(parse_run_id);

CREATE TABLE chunks (
    pk            INTEGER PRIMARY KEY,            -- stable rowid for the FTS external content
    id            TEXT NOT NULL UNIQUE,           -- content-derived stable ID
    parse_run_id  TEXT NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    section_id    TEXT REFERENCES sections(id) ON DELETE CASCADE,
    paper_id      TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                  -- text | caption | references
    order_idx     INTEGER NOT NULL,
    section_path  TEXT,
    text          TEXT NOT NULL,
    token_count   INTEGER NOT NULL,
    page_start    INTEGER NOT NULL,               -- 1-based PDF page index
    page_end      INTEGER NOT NULL,
    bbox_json     TEXT NOT NULL DEFAULT '[]',
    quality_flags TEXT NOT NULL DEFAULT '[]',
    hash          TEXT NOT NULL
);
CREATE INDEX idx_chunks_run ON chunks(parse_run_id);
CREATE INDEX idx_chunks_paper ON chunks(paper_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, section_path,
    content='chunks', content_rowid='pk',
    tokenize='porter unicode61'
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section_path) VALUES (new.pk, new.text, new.section_path);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_path)
    VALUES ('delete', old.pk, old.text, old.section_path);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_path)
    VALUES ('delete', old.pk, old.text, old.section_path);
    INSERT INTO chunks_fts(rowid, text, section_path) VALUES (new.pk, new.text, new.section_path);
END;

CREATE TABLE assets (
    id           TEXT PRIMARY KEY,
    parse_run_id TEXT NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,                   -- figure | table | equation
    label        TEXT,
    caption      TEXT,
    page         INTEGER NOT NULL,
    bbox_json    TEXT,
    path         TEXT,                            -- relative to data root
    data_json    TEXT,                            -- table cells etc.
    quality      TEXT NOT NULL                    -- verified | uncertain | failed
);
CREATE INDEX idx_assets_run ON assets(parse_run_id);

CREATE TABLE projects (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    topic             TEXT,
    research_question TEXT,
    criteria_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);

CREATE TABLE project_papers (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id   TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source     TEXT NOT NULL,                     -- import | discovery | manual
    added_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, paper_id)
);
CREATE INDEX idx_project_papers_paper ON project_papers(paper_id);

CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,                    -- ingest | parse | index
    target_id   TEXT,
    status      TEXT NOT NULL,                    -- running | done | failed | skipped
    detail_json TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_jobs_target ON jobs(kind, target_id);
