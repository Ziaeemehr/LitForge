ALTER TABLE watch_runs ADD COLUMN owner_pid INTEGER;
ALTER TABLE watch_runs ADD COLUMN owner_started_at TEXT;
ALTER TABLE watch_runs ADD COLUMN owner_process_started_at TEXT;
ALTER TABLE console_runs ADD COLUMN owner_pid INTEGER;
ALTER TABLE console_runs ADD COLUMN owner_started_at TEXT;
ALTER TABLE console_runs ADD COLUMN owner_process_started_at TEXT;
