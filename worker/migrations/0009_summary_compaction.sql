-- Mark channel summaries that have been compacted into a higher-level summary
-- (monthly or yearly).  Compacted summaries are retained for audit and retry
-- safety but are removed from the active set and the canonical RAG document.

ALTER TABLE channel_daily_summaries ADD COLUMN compacted INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_channel_daily_compacted
    ON channel_daily_summaries(compacted, updated_at);
CREATE INDEX IF NOT EXISTS idx_channel_daily_active
    ON channel_daily_summaries(owner_sub, conversation_id, summary_type, period_start, compacted);
