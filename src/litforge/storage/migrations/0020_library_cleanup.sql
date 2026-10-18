-- 0020: Unreferenced evidence can be collected with unused library papers.
-- Include nested payloads and JSON encoded inside JSON strings: human_reviews
-- serializes complete rows, so evidence_ids_json is itself an encoded string.
-- UNION deduplicates payloads and terminates recursive expansion. Keep this view
-- in sync when adding tables/columns that can store evidence IDs. Repro links
-- are removable cache rows; the trigger requires their explicit deletion first.
-- Cleanup evaluates this view once under BEGIN IMMEDIATE, then authorizes only
-- verified IDs. Per-row triggers use indexed lookups, never JSON traversal.
CREATE VIEW library_evidence_references AS
WITH RECURSIVE payloads(value) AS (
SELECT config_json FROM analysis_runs WHERE json_valid(config_json)
UNION
SELECT result_json FROM analysis_runs WHERE json_valid(result_json)
UNION
SELECT dropped_evidence_ids_json FROM analysis_runs WHERE json_valid(dropped_evidence_ids_json)
UNION
SELECT evidence_ids_json FROM answer_sentences WHERE json_valid(evidence_ids_json)
UNION
SELECT flags_json FROM answer_sentences WHERE json_valid(flags_json)
UNION
SELECT quotes_json FROM answer_sentences WHERE json_valid(quotes_json)
UNION
SELECT result_json FROM asset_analyses WHERE json_valid(result_json)
UNION
SELECT fidelity_json FROM asset_analyses WHERE json_valid(fidelity_json)
UNION
SELECT bbox_json FROM assets WHERE json_valid(bbox_json)
UNION
SELECT data_json FROM assets WHERE json_valid(data_json)
UNION
SELECT bbox_json FROM chunks WHERE json_valid(bbox_json)
UNION
SELECT causes_json FROM claim_relations WHERE json_valid(causes_json)
UNION
SELECT old_json FROM claim_reviews WHERE json_valid(old_json)
UNION
SELECT new_json FROM claim_reviews WHERE json_valid(new_json)
UNION
SELECT plan_json FROM console_runs WHERE json_valid(plan_json)
UNION
SELECT results_json FROM console_runs WHERE json_valid(results_json)
UNION
SELECT reasons_json FROM discovery_results WHERE json_valid(reasons_json)
UNION
SELECT components_json FROM discovery_results WHERE json_valid(components_json)
UNION
SELECT params_json FROM discovery_runs WHERE json_valid(params_json)
UNION
SELECT stats_json FROM discovery_runs WHERE json_valid(stats_json)
UNION
SELECT result_json FROM fulltext_screening WHERE json_valid(result_json)
UNION
SELECT evidence_ids_json FROM fulltext_screening WHERE json_valid(evidence_ids_json)
UNION
SELECT evidence_ids_json FROM gap_candidates WHERE json_valid(evidence_ids_json)
UNION
SELECT content_json FROM gap_cards WHERE json_valid(content_json)
UNION
SELECT old_json FROM human_reviews WHERE json_valid(old_json)
UNION
SELECT new_json FROM human_reviews WHERE json_valid(new_json)
UNION
SELECT detail_json FROM jobs WHERE json_valid(detail_json)
UNION
SELECT columns_json FROM matrices WHERE json_valid(columns_json)
UNION
SELECT value_json FROM matrix_cells WHERE json_valid(value_json)
UNION
SELECT evidence_ids_json FROM matrix_cells WHERE json_valid(evidence_ids_json)
UNION
SELECT flags_json FROM matrix_cells WHERE json_valid(flags_json)
UNION
SELECT human_value_json FROM matrix_cells WHERE json_valid(human_value_json)
UNION
SELECT evidence_ids_json FROM model_calls WHERE json_valid(evidence_ids_json)
UNION
SELECT dropped_evidence_ids_json FROM model_calls WHERE json_valid(dropped_evidence_ids_json)
UNION
SELECT authors_json FROM papers WHERE json_valid(authors_json)
UNION
SELECT metadata_json FROM papers WHERE json_valid(metadata_json)
UNION
SELECT quality_json FROM parse_runs WHERE json_valid(quality_json)
UNION
SELECT data_json FROM project_snapshots WHERE json_valid(data_json)
UNION
SELECT criteria_json FROM projects WHERE json_valid(criteria_json)
UNION
SELECT result_json FROM screening_results WHERE json_valid(result_json)
UNION
SELECT delta_json FROM watch_runs WHERE json_valid(delta_json)
UNION
SELECT signals_json FROM watch_runs WHERE json_valid(signals_json)
UNION
SELECT j.value FROM payloads p, json_tree(p.value) j
WHERE j.type = 'text' AND json_valid(j.value)
)
SELECT evidence_id FROM run_evidence
UNION
SELECT evidence_id FROM claim_evidence
UNION
SELECT e.id FROM payloads p, json_tree(p.value) j
JOIN evidence_spans e ON e.id = j.atom
WHERE j.type = 'text';

CREATE TABLE evidence_span_deletions (id TEXT PRIMARY KEY);
CREATE INDEX idx_run_evidence_evidence ON run_evidence(evidence_id);
CREATE INDEX idx_claim_evidence_evidence ON claim_evidence(evidence_id);
CREATE INDEX idx_repro_links_evidence ON repro_links(evidence_id);

DROP TRIGGER evidence_spans_no_delete;
CREATE TRIGGER evidence_spans_no_delete BEFORE DELETE ON evidence_spans
WHEN NOT EXISTS (SELECT 1 FROM evidence_span_deletions WHERE id = OLD.id)
  OR EXISTS (SELECT 1 FROM run_evidence WHERE evidence_id = OLD.id)
  OR EXISTS (SELECT 1 FROM claim_evidence WHERE evidence_id = OLD.id)
  OR EXISTS (SELECT 1 FROM repro_links WHERE evidence_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'evidence spans are immutable'); END;

-- Screening history remains append-only outside verified library cleanup.
CREATE TABLE library_paper_deletions (id TEXT PRIMARY KEY);
DROP TRIGGER screening_history_no_delete;
CREATE TRIGGER screening_history_no_delete BEFORE DELETE ON screening_history
WHEN NOT EXISTS (SELECT 1 FROM library_paper_deletions WHERE id = OLD.paper_id)
BEGIN SELECT RAISE(ABORT, 'screening history is append-only'); END;
CREATE INDEX idx_screening_history_paper_id ON screening_history(paper_id);
CREATE INDEX idx_claim_reviews_claim ON claim_reviews(claim_id);
