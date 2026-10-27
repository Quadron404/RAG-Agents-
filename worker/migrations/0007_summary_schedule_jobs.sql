-- Daily-summary scheduling job state (additive).
-- One job per (owner, channel, local date).  Unique index prevents duplicates;
-- status/attempts/lease support claim, retry, and recovery of interrupted work.
CREATE TABLE IF NOT EXISTS summary_schedule_jobs (
    job_id          TEXT PRIMARY KEY,
    owner_sub       TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    local_date      TEXT NOT NULL,          -- YYYY-MM-DD in the owner's timezone
    summary_type    TEXT NOT NULL DEFAULT 'daily',
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | claimed | completed | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    available_at    INTEGER NOT NULL,
    lease_until     INTEGER,
    error_code      TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_schedule_unique
    ON summary_schedule_jobs(owner_sub, conversation_id, local_date, summary_type);
CREATE INDEX IF NOT EXISTS idx_summary_schedule_due
    ON summary_schedule_jobs(status, available_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_summary_schedule_owner_conv
    ON summary_schedule_jobs(owner_sub, conversation_id, local_date);
