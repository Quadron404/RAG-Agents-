-- 30-day roll-up job state (additive).
-- Reuses summary_schedule_jobs with summary_type = 'rollup_30d'.
-- Additional columns track period boundaries and input summary versions
-- so changed/obsolete inputs can be detected and obsolete roll-ups replaced.
ALTER TABLE summary_schedule_jobs ADD COLUMN period_start TEXT NOT NULL DEFAULT '';
ALTER TABLE summary_schedule_jobs ADD COLUMN period_end TEXT NOT NULL DEFAULT '';
ALTER TABLE summary_schedule_jobs ADD COLUMN input_summary_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE summary_schedule_jobs ADD COLUMN input_summary_versions TEXT NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS idx_summary_schedule_rollup
    ON summary_schedule_jobs(owner_sub, conversation_id, summary_type, period_start);
