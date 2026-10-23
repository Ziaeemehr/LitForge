-- English search terms for a question written in Persian, one row per question text and model.
CREATE TABLE query_translations (
    text_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    english TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_hash, model)
);
