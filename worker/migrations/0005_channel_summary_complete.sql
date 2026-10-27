-- Complete the daily / 30-day channel summary schema (additive).
-- Extends channel_daily_summaries instead of duplicating it.

ALTER TABLE channel_daily_summaries ADD COLUMN local_date TEXT NOT NULL DEFAULT '';
ALTER TABLE channel_daily_summaries ADD COLUMN generation_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE channel_daily_summaries ADD COLUMN source_revision TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_channel_daily_generation
    ON channel_daily_summaries(generation_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_channel_daily_localdate
    ON channel_daily_summaries(owner_sub, conversation_id, local_date);