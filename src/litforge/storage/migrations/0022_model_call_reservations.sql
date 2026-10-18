CREATE TABLE model_call_reservations (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
    created_at TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    owner_started_at TEXT NOT NULL,
    owner_process_started_at TEXT
);
CREATE INDEX model_call_reservations_run ON model_call_reservations(run_id);
