-- Worker-owned conversations.
--
-- The chat transcript is edge state now: the browser talks to the Worker, the
-- Worker stores the transcript in D1, and only the computer-use features still
-- reach the Codespace.  These tables are therefore keyed by the authenticated
-- Google subject (owner_sub) and every accessor filters on it.

CREATE TABLE IF NOT EXISTS conversations (
    id                 TEXT PRIMARY KEY,
    owner_sub          TEXT NOT NULL,
    owner_id           TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT 'New chat',
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    -- The inactivity timer is measured from the end of the last turn, not from
    -- the start of the last request.
    last_activity_at   INTEGER NOT NULL,
    -- The sequence number of the last message covered by a summary.  A summary
    -- only moves this forward, so a slow job can never erase newer coverage.
    summarized_through INTEGER NOT NULL DEFAULT 0,
    -- idle | pending | running | failed
    summary_status     TEXT NOT NULL DEFAULT 'idle',
    summary_due_at     INTEGER,
    -- One generation per conversation.  A second concurrent browser turn is
    -- refused instead of being allowed to interleave transcript writes.
    active_request_id  TEXT NOT NULL DEFAULT '',
    active_since       INTEGER,
    deleted_at         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_conversations_owner
    ON conversations(owner_sub, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_summary
    ON conversations(summary_status, last_activity_at);

CREATE TABLE IF NOT EXISTS messages (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    owner_sub       TEXT NOT NULL,
    -- user | assistant | system | tool
    role            TEXT NOT NULL,
    agent           TEXT NOT NULL DEFAULT 'chief',
    content         TEXT NOT NULL DEFAULT '',
    -- JSON metadata.  Tool calls/results live here or on hidden tool rows; the
    -- read API never exposes raw tool messages as chat bubbles.
    data            TEXT NOT NULL DEFAULT '{}',
    hidden          INTEGER NOT NULL DEFAULT 0,
    request_id      TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_owner
    ON messages(owner_sub, conversation_id, seq);
-- An idempotent request owns exactly one user message and one assistant message.
-- The partial indexes allow hidden tool rows to share the request without
-- tripping the uniqueness that protects retries.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_request_user
    ON messages(request_id) WHERE role = 'user' AND hidden = 0 AND request_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_request_assistant
    ON messages(request_id) WHERE role = 'assistant' AND hidden = 0 AND request_id <> '';

CREATE TABLE IF NOT EXISTS chat_requests (
    request_id            TEXT PRIMARY KEY,
    conversation_id       TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    owner_sub             TEXT NOT NULL,
    user_message_id       TEXT NOT NULL,
    assistant_message_id  TEXT NOT NULL,
    -- streaming | completed | failed
    status                TEXT NOT NULL DEFAULT 'streaming',
    error                 TEXT NOT NULL DEFAULT '',
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_requests_conversation
    ON chat_requests(conversation_id, updated_at);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    owner_sub       TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    covered_from    INTEGER NOT NULL DEFAULT 0,
    covered_to      INTEGER NOT NULL,
    doc_key         TEXT NOT NULL DEFAULT '',
    -- pending | indexed | failed | unconfigured
    index_status    TEXT NOT NULL DEFAULT 'pending',
    index_error     TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    indexed_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_conversation_summaries_index
    ON conversation_summaries(index_status, updated_at);

CREATE TABLE IF NOT EXISTS summary_jobs (
    job_id          TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    owner_sub       TEXT NOT NULL,
    covered_to      INTEGER NOT NULL,
    -- pending | running | completed | failed
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    available_at    INTEGER NOT NULL,
    lease_until     INTEGER,
    error_code      TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_jobs_due
    ON summary_jobs(status, available_at);
CREATE INDEX IF NOT EXISTS idx_summary_jobs_conversation
    ON summary_jobs(conversation_id, status);
