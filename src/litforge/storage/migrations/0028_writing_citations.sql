-- Which paper (and, when cited from the evidence ledger, which quoted span) each \cite key in a document came from.
CREATE TABLE writing_citations (
    id          INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES writing_documents(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    paper_id    TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    evidence_id TEXT REFERENCES evidence_spans(id),
    added_at    TEXT NOT NULL
);
CREATE INDEX idx_writing_citations_doc ON writing_citations(document_id, key);
