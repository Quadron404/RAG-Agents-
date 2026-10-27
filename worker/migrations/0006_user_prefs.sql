-- User preferences persisted at the edge: the owner's IANA timezone and the
-- Developer Mode flag.  Both are additive columns on the existing users table.
ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE users ADD COLUMN developer_mode INTEGER NOT NULL DEFAULT 1;
