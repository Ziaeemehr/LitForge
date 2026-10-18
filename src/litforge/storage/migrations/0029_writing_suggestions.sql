-- Model suggestions offered in the Writing Studio, kept for the record whether accepted or not.
CREATE TABLE writing_suggestions (
    id             TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES writing_documents(id) ON DELETE CASCADE,
    task           TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    file           TEXT NOT NULL,
    original       TEXT NOT NULL,
    hunks_json     TEXT NOT NULL,
    flags_json     TEXT NOT NULL DEFAULT '[]',
    citations_json TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'partial', 'rejected')),
    accepted_json  TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    decided_at     TEXT
);
CREATE INDEX idx_writing_suggestions_doc ON writing_suggestions(document_id, created_at);
