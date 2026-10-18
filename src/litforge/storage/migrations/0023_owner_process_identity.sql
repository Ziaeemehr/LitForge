ALTER TABLE watch_runs ADD COLUMN owner_process_started_at TEXT;
ALTER TABLE console_runs ADD COLUMN owner_process_started_at TEXT;
ALTER TABLE model_call_reservations ADD COLUMN owner_process_started_at TEXT;
