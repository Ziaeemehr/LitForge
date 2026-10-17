CREATE TRIGGER evidence_spans_no_reinsert BEFORE INSERT ON evidence_spans
WHEN EXISTS (SELECT 1 FROM evidence_spans WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'evidence spans are immutable'); END;

ALTER TABLE run_evidence ADD COLUMN page INTEGER;
