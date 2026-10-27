-- Connector approvals: pending Allow/Deny cards for connector-backed actions.
-- Stored at the edge (D1), never in the Codespace backend.  A model tool call
-- is only a request; the Worker creates a one-time pending approval here, the
-- UI shows the Allow/Deny card, and only after the owner clicks Allow does the
-- Worker execute the action.
--
-- Identity is owner_scoped: user_id comes from the edge session, never trusted
-- from the client.  One row per approval, single-use -- once decided (allowed,
-- denied, expired) it cannot be replayed.
CREATE TABLE IF NOT EXISTS connector_approvals (
    approval_id    TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    owner_sub      TEXT NOT NULL,
    connector      TEXT NOT NULL,
    action         TEXT NOT NULL,
    arguments      TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    decided_at     INTEGER,
    decided_by     TEXT,
    result         TEXT,
    routine_id     TEXT,
    routine_run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_connector_approvals_user ON connector_approvals(user_id, status);
CREATE INDEX IF NOT EXISTS idx_connector_approvals_expires ON connector_approvals(expires_at) WHERE status = 'pending';
