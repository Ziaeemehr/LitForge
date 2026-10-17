-- REPLACE can bypass delete triggers when recursive_triggers is off (SQLite's default).
-- Check both unique identities before SQLite can delete the existing row.
DROP TRIGGER evidence_spans_no_reinsert;
CREATE TRIGGER evidence_spans_no_reinsert BEFORE INSERT ON evidence_spans
WHEN EXISTS (SELECT 1 FROM evidence_spans WHERE id=NEW.id OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'evidence spans are immutable'); END;

DROP TRIGGER human_reviews_no_reinsert;
CREATE TRIGGER human_reviews_no_reinsert BEFORE INSERT ON human_reviews
WHEN EXISTS (SELECT 1 FROM human_reviews WHERE id=NEW.id OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'human reviews are append-only'); END;
