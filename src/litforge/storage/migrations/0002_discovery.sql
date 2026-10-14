-- 0002: paper discovery (multi-source search, citation chaining, reference imports).

ALTER TABLE papers ADD COLUMN arxiv_id TEXT;        -- without version suffix
ALTER TABLE papers ADD COLUMN openalex_id TEXT;     -- e.g. W2984417636
ALTER TABLE papers ADD COLUMN s2_id TEXT;
ALTER TABLE papers ADD COLUMN venue TEXT;
ALTER TABLE papers ADD COLUMN cited_by_count INTEGER;
ALTER TABLE papers ADD COLUMN oa_url TEXT;          -- legal open-access PDF/landing URL, if known
ALTER TABLE papers ADD COLUMN norm_title TEXT;      -- normalized title for matching
CREATE INDEX idx_papers_arxiv ON papers(arxiv_id);
CREATE INDEX idx_papers_openalex ON papers(openalex_id);
CREATE INDEX idx_papers_s2 ON papers(s2_id);
CREATE INDEX idx_papers_norm_title ON papers(norm_title);

CREATE TABLE discovery_runs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                     -- search | citations | import
    params_json TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,                     -- running | done | partial | failed
    stats_json  TEXT NOT NULL DEFAULT '{}',        -- per-source counts, errors, cost
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_discovery_runs_project ON discovery_runs(project_id);

-- One row per (source, query/seed) result: why each paper was found.
CREATE TABLE discovery_hits (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
    paper_id      TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,                   -- openalex | crossref | arxiv | s2 | import:<file>
    source_id     TEXT,
    query         TEXT,
    relation      TEXT NOT NULL,                   -- query | references | cited_by | seed | import
    seed_paper_id TEXT REFERENCES papers(id) ON DELETE SET NULL,
    rank          INTEGER
);
CREATE INDEX idx_discovery_hits_run ON discovery_hits(run_id);
CREATE INDEX idx_discovery_hits_paper ON discovery_hits(paper_id);

-- Aggregated ranking per run (deterministic; see discovery/rank.py).
CREATE TABLE discovery_results (
    run_id         TEXT NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
    paper_id       TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    score          REAL NOT NULL,
    n_hits         INTEGER NOT NULL,
    new_to_project INTEGER NOT NULL,
    PRIMARY KEY (run_id, paper_id)
);

CREATE TABLE paper_relations (
    citing_id  TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    cited_id   TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (citing_id, cited_id)
);
CREATE INDEX idx_paper_relations_cited ON paper_relations(cited_id);
