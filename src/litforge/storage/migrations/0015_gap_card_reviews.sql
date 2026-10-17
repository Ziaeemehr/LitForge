DROP TRIGGER human_reviews_no_update;
DROP TRIGGER human_reviews_no_delete;
DROP TRIGGER human_reviews_no_reinsert;
DROP INDEX idx_human_reviews_target;

ALTER TABLE human_reviews RENAME TO human_reviews_previous;

CREATE TABLE human_reviews (
    id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('sentence','cell','gap_card')),
    target_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('accept','reject','edit')),
    old_json TEXT NOT NULL,
    new_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
INSERT INTO human_reviews(id,target_kind,target_id,action,old_json,new_json,reason,created_at)
SELECT id,target_kind,target_id,action,old_json,new_json,reason,created_at FROM human_reviews_previous;
DROP TABLE human_reviews_previous;

CREATE TRIGGER human_reviews_no_update BEFORE UPDATE ON human_reviews
BEGIN SELECT RAISE(ABORT, 'human reviews are append-only'); END;
CREATE TRIGGER human_reviews_no_delete BEFORE DELETE ON human_reviews
BEGIN SELECT RAISE(ABORT, 'human reviews are append-only'); END;
CREATE TRIGGER human_reviews_no_reinsert BEFORE INSERT ON human_reviews
WHEN EXISTS (SELECT 1 FROM human_reviews WHERE id=NEW.id OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'human reviews are append-only'); END;
CREATE INDEX idx_human_reviews_target ON human_reviews(target_kind,target_id);
