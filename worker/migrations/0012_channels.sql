-- First-class channel entity and conversation membership.
-- Channels organize conversations; Main exists on first use.
-- Legacy conversations without a channel_id are attached to Main during migration.

CREATE TABLE IF NOT EXISTS channels (
    id                 TEXT PRIMARY KEY,
    owner_sub          TEXT NOT NULL,
    name               TEXT NOT NULL,
    type               TEXT NOT NULL DEFAULT 'project',  -- main | project
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    archived_at        INTEGER,
    sort_order         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_channels_owner
    ON channels(owner_sub, sort_order ASC, created_at ASC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_owner_main
    ON channels(owner_sub)
    WHERE type = 'main';

-- Add channel_id to conversations.  Default to Main via migration below.
ALTER TABLE conversations ADD COLUMN channel_id TEXT REFERENCES channels(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_channel
    ON conversations(channel_id, updated_at DESC);

-- Migration: ensure every owner has a Main channel and attach legacy conversations.
-- This is written as a one-off script block; the Worker applies it on first run.
-- See worker/src/store.ts migrateChannels().

-- Channel daily summaries already reference conversation_id.
-- Add a view for convenient channel-scoped summary queries.
CREATE VIEW IF NOT EXISTS channel_conversation_summaries AS
SELECT
    c.id AS channel_id,
    c.owner_sub,
    c.name AS channel_name,
    c.type AS channel_type,
    c.archived_at,
    c.sort_order,
    conv.id AS conversation_id,
    conv.title,
    conv.updated_at AS conversation_updated_at,
    conv.summarized_through,
    conv.summary_status
FROM channels c
JOIN conversations conv ON conv.channel_id = c.id
WHERE conv.deleted_at IS NULL;