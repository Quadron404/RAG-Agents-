-- Durable routines: the schedule and the run ledger live at the edge.
--
-- This is deliberately NOT in the Codespace backend's SQLite.  A routine is
-- supposed to fire "while the Codespace is stopped", and a database that only
-- exists while the Codespace runs cannot answer "which routines are due?" at the
-- moment that matters.  D1 is always on, so it is the authority for the *when*
-- and the *what ran*; the backend is where the *work* happens once it is awake.
--
-- Identity is the Google subject (owner_sub), not the backend user id: the edge
-- knows who is signed in even when the backend is down.  owner_id is carried
-- along so a wake-and-dispatch request can address the right backend user the
-- moment the backend is reachable again.

CREATE TABLE IF NOT EXISTS routines (
    routine_id          TEXT PRIMARY KEY,
    owner_sub           TEXT NOT NULL,
    owner_id            TEXT NOT NULL DEFAULT '',
    agent_id            TEXT NOT NULL DEFAULT '',
    name                TEXT NOT NULL DEFAULT '',
    task_prompt         TEXT NOT NULL DEFAULT '',
    schedule_type       TEXT NOT NULL,
    schedule_definition TEXT NOT NULL DEFAULT '{}',
    timezone            TEXT NOT NULL DEFAULT 'UTC',
    enabled             INTEGER NOT NULL DEFAULT 1,
    -- Epoch milliseconds, always UTC.  The timezone above only ever affects how
    -- the next instant is computed from a wall-clock schedule.
    next_run_at         INTEGER,
    last_run_at         INTEGER,
    last_run_status     TEXT NOT NULL DEFAULT '',
    created_at          INTEGER,
    updated_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_routines_owner ON routines(owner_sub, routine_id);
CREATE INDEX IF NOT EXISTS idx_routines_due ON routines(enabled, next_run_at);

-- One row per materialised execution.  A run is written *before* it is
-- dispatched, and its idempotency_key is the routine id plus the instant it was
-- scheduled for, so the same occurrence can never produce two runs even if the
-- scheduler is retried, the Worker restarts or two isolates race on one tick.
CREATE TABLE IF NOT EXISTS routine_runs (
    routine_run_id   TEXT PRIMARY KEY,
    routine_id       TEXT NOT NULL,
    owner_sub        TEXT NOT NULL,
    scheduled_for    INTEGER NOT NULL,
    -- pending  -> materialised, not yet dispatched
    -- queued   -> dispatch attempted, the backend was not reachable yet
    -- running  -> leased to a dispatcher right now
    -- delayed  -> a retryable failure (e.g. provider rate limit); retry later
    -- completed-> the backend returned a real result
    -- failed   -> non-retryable, or the attempt budget was spent
    -- rejected -> the routine was paused/deleted before the run dispatched
    status           TEXT NOT NULL DEFAULT 'pending',
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    lease_until      INTEGER,
    available_at     INTEGER,
    started_at       INTEGER,
    completed_at     INTEGER,
    result_summary   TEXT NOT NULL DEFAULT '',
    error_code       TEXT NOT NULL DEFAULT '',
    idempotency_key  TEXT NOT NULL UNIQUE,
    approval_id      TEXT NOT NULL DEFAULT '',
    created_at       INTEGER,
    updated_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_routine_runs_dispatch ON routine_runs(status, available_at);
CREATE INDEX IF NOT EXISTS idx_routine_runs_routine ON routine_runs(routine_id, scheduled_for);
