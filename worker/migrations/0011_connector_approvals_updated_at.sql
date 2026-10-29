-- Add updated_at column to connector_approvals and backfill existing rows
-- from created_at so existing approvals retain a valid timestamp.
ALTER TABLE connector_approvals ADD COLUMN updated_at INTEGER;

UPDATE connector_approvals SET updated_at = created_at WHERE updated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_connector_approvals_updated ON connector_approvals(updated_at);
