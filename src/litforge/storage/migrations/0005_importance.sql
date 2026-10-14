-- 0005: impact metrics for importance scoring and review priority.

ALTER TABLE papers ADD COLUMN fwci REAL;                 -- field-weighted citation impact (OpenAlex)
ALTER TABLE papers ADD COLUMN citation_percentile REAL;  -- 0..1, normalized by field and year (OpenAlex)
ALTER TABLE papers ADD COLUMN recent_citations INTEGER;  -- citations in the last two calendar years
ALTER TABLE papers ADD COLUMN is_retracted INTEGER;
ALTER TABLE papers ADD COLUMN venue_id TEXT;             -- OpenAlex venue id, e.g. S125754415

-- Venue-level impact, fetched once per venue.
CREATE TABLE venues (
    id             TEXT PRIMARY KEY,                 -- OpenAlex source id
    name           TEXT,
    type           TEXT,
    mean_citedness REAL,                             -- 2-year mean citedness (impact-factor analogue)
    h_index        INTEGER,
    updated_at     TEXT NOT NULL
);

-- What each paper cites (OpenAlex ids), for centrality within a result set.
CREATE TABLE paper_references (
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    ref_id   TEXT NOT NULL,
    PRIMARY KEY (paper_id, ref_id)
);
CREATE INDEX idx_paper_references_ref ON paper_references(ref_id);

ALTER TABLE discovery_results ADD COLUMN importance REAL;
ALTER TABLE discovery_results ADD COLUMN priority REAL;
