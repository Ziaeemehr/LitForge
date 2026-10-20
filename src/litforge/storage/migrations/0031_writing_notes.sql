CREATE TABLE writing_notes (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES writing_documents(id) ON DELETE CASCADE,
    file TEXT NOT NULL,
    line INTEGER NOT NULL CHECK(line >= 1),
    anchor TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX writing_notes_document_file ON writing_notes(document_id, file);
