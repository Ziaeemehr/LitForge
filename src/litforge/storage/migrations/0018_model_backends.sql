ALTER TABLE model_calls ADD COLUMN reasoning TEXT NOT NULL DEFAULT 'off';
ALTER TABLE model_calls ADD COLUMN dropped_evidence_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE analysis_runs ADD COLUMN dropped_evidence_ids_json TEXT NOT NULL DEFAULT '[]';
