-- Daily and 30-day roll-up summaries, scoped per channel (conversation).
CREATE TABLE IF NOT EXISTS channel_daily_summaries (
    id              TEXT NOT NULL PRIMARY KEY,
    owner_sub       TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    summary_type    TEXT NOT NULL,          -- daily | rollup_30d
    period_start    TEXT NOT NULL,          -- ISO date or 30-day cycle key
    period_end      TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    covered_from_seq INTEGER NOT NULL DEFAULT 0,
    covered_to_seq   INTEGER NOT NULL DEFAULT 0,
    source_summary_ids TEXT NOT NULL DEFAULT '[]',
    index_status    TEXT NOT NULL DEFAULT 'pending',
    index_error     TEXT NOT NULL DEFAULT '',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    indexed_at      INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_daily_unique
    ON channel_daily_summaries(owner_sub, conversation_id, summary_type, period_start);
CREATE INDEX IF NOT EXISTS idx_channel_daily_owner_conv
    ON channel_daily_summaries(owner_sub, conversation_id, period_start);
CREATE INDEX IF NOT EXISTS idx_channel_daily_index
    ON channel_daily_summaries(index_status, updated_at);
