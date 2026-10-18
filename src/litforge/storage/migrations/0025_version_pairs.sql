-- Branch databases may already have 0024. Collapse raced two-member suggestions before adding
-- uniqueness, keeping a remembered rejection ahead of any pending copy of the same pair.
DELETE FROM paper_versions WHERE confirmed=0 AND group_id IN (
    SELECT group_id FROM (
        SELECT group_id, ROW_NUMBER() OVER (
            PARTITION BY MIN(paper_id),MAX(paper_id)
            ORDER BY MAX(rejected) DESC,MIN(created_at),group_id
        ) AS pair_rank
        FROM paper_versions WHERE confirmed=0 GROUP BY group_id HAVING COUNT(*)=2
    ) WHERE pair_rank>1
);

-- A group is stored as membership rows, so a separate normalized pair index enforces uniqueness
-- across groups. Triggers keep it in sync for suggestions, rejections, confirmations and deletions.
CREATE TABLE paper_version_pairs (
    paper_id_a TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    paper_id_b TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL,
    CHECK (paper_id_a < paper_id_b),
    PRIMARY KEY (paper_id_a,paper_id_b)
);
CREATE INDEX idx_paper_version_pairs_group ON paper_version_pairs(group_id);
INSERT INTO paper_version_pairs(paper_id_a,paper_id_b,group_id)
    SELECT x.paper_id,y.paper_id,x.group_id
    FROM paper_versions x JOIN paper_versions y ON x.group_id=y.group_id
    WHERE x.confirmed=0 AND y.confirmed=0 AND x.paper_id<y.paper_id;

CREATE TRIGGER paper_version_pairs_insert AFTER INSERT ON paper_versions WHEN NEW.confirmed=0
BEGIN
    -- RAISE also prevents an outer INSERT OR IGNORE/REPLACE from bypassing this constraint.
    SELECT RAISE(ABORT,'duplicate version pair') FROM paper_versions member
        JOIN paper_version_pairs pair
          ON pair.paper_id_a=MIN(NEW.paper_id,member.paper_id)
         AND pair.paper_id_b=MAX(NEW.paper_id,member.paper_id)
        WHERE NEW.confirmed=0 AND member.confirmed=0 AND member.group_id=NEW.group_id
          AND member.paper_id<>NEW.paper_id AND pair.group_id<>NEW.group_id;
    INSERT INTO paper_version_pairs(paper_id_a,paper_id_b,group_id)
        SELECT MIN(NEW.paper_id,paper_id),MAX(NEW.paper_id,paper_id),NEW.group_id
        FROM paper_versions WHERE group_id=NEW.group_id AND confirmed=0 AND paper_id<>NEW.paper_id;
END;
CREATE TRIGGER paper_version_pairs_delete AFTER DELETE ON paper_versions
BEGIN
    DELETE FROM paper_version_pairs WHERE group_id=OLD.group_id
        AND (paper_id_a=OLD.paper_id OR paper_id_b=OLD.paper_id);
END;
CREATE TRIGGER paper_version_pairs_update AFTER UPDATE OF group_id,paper_id,confirmed ON paper_versions
BEGIN
    DELETE FROM paper_version_pairs WHERE group_id=OLD.group_id
        AND (paper_id_a=OLD.paper_id OR paper_id_b=OLD.paper_id);
    -- RAISE also prevents an outer INSERT OR IGNORE/REPLACE from bypassing this constraint.
    SELECT RAISE(ABORT,'duplicate version pair') FROM paper_versions member
        JOIN paper_version_pairs pair
          ON pair.paper_id_a=MIN(NEW.paper_id,member.paper_id)
         AND pair.paper_id_b=MAX(NEW.paper_id,member.paper_id)
        WHERE NEW.confirmed=0 AND member.confirmed=0 AND member.group_id=NEW.group_id
          AND member.paper_id<>NEW.paper_id AND pair.group_id<>NEW.group_id;
    INSERT INTO paper_version_pairs(paper_id_a,paper_id_b,group_id)
        SELECT MIN(NEW.paper_id,paper_id),MAX(NEW.paper_id,paper_id),NEW.group_id
        FROM paper_versions WHERE NEW.confirmed=0 AND group_id=NEW.group_id AND confirmed=0
            AND paper_id<>NEW.paper_id;
END;
