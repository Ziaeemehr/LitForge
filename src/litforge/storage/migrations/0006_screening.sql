-- 0006: title/abstract screening by a local LLM. One result per (project, paper): a paper found by several
-- runs is screened once. criteria_hash tells the UI a result predates the current criteria.

CREATE TABLE screening_results (
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id       TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    decision       TEXT NOT NULL,                 -- include | uncertain | exclude
    score          REAL NOT NULL,                 -- 0..1, computed in Python
    result_json    TEXT NOT NULL,                 -- Decision: per-criterion status, evidence, reason
    error          TEXT,                          -- set when the model gave no usable answer
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    criteria_hash  TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,                 -- prompt + model + criteria + title + abstract
    screened_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, paper_id)
);
