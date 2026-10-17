-- 0016: 1 when the latest gap detection no longer finds a candidate (kept for history, not current).
ALTER TABLE gap_candidates ADD COLUMN stale INTEGER NOT NULL DEFAULT 0;
