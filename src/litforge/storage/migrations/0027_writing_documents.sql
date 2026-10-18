-- Writing Studio documents. The files in each document's folder are the source of truth; this row names them.
CREATE TABLE writing_documents (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    slug       TEXT NOT NULL,
    template   TEXT NOT NULL,
    engine     TEXT NOT NULL DEFAULT 'tectonic' CHECK (engine IN ('tectonic', 'xelatex')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, slug)
);
