-- 0003: explainable ranking for seed-based similar-paper search.

ALTER TABLE discovery_results ADD COLUMN reasons_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE discovery_results ADD COLUMN components_json TEXT NOT NULL DEFAULT '{}';
