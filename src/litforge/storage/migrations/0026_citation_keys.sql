-- One BibTeX key per paper, created on first use and never changed unless the user sets it.
CREATE TABLE citation_keys (
    paper_id   TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    key        TEXT NOT NULL UNIQUE,
    user_set   INTEGER NOT NULL DEFAULT 0 CHECK (user_set IN (0, 1)),
    created_at TEXT NOT NULL
);
