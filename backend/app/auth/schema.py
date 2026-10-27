"""Schema for identity, ownership, the durable control plane and email.

Kept separate from the original three tables (threads/messages/memory) so the
legacy conversation store is untouched.  Every table is created with
``IF NOT EXISTS`` and every statement here is safe to re-run, which is what
makes the migration repeatable against both a fresh and an existing database.

Ownership columns are non-null on every user-scoped table and the queries in the
store modules always filter on them.  Foreign keys are declared for
documentation and enforced where SQLite allows (``PRAGMA foreign_keys`` is on
during init), but the authorization checks do not rely on them alone.
"""

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    google_sub TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL DEFAULT '',
    email_verified INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    picture TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at REAL,
    updated_at REAL,
    -- The thread this user last had open.  Restored on app open/refresh so the
    -- conversation list and composer pick up where the user left off.  NULL
    -- means "no active thread"; the UI must not invent one.
    last_active_thread_id TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at REAL,
    expires_at REAL,
    revoked INTEGER NOT NULL DEFAULT 0,
    rotated_from TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    code_verifier TEXT NOT NULL DEFAULT '',
    nonce TEXT NOT NULL DEFAULT '',
    redirect_uri TEXT NOT NULL DEFAULT '',
    created_at REAL,
    expires_at REAL,
    consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL
);
"""

CONTROL_PLANE_SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_connections (
    user_id TEXT PRIMARY KEY,
    google_sub TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    refresh_token_enc TEXT NOT NULL DEFAULT '',
    access_token_enc TEXT NOT NULL DEFAULT '',
    token_expiry REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'disconnected',
    last_error TEXT NOT NULL DEFAULT '',
    plus_addressing INTEGER NOT NULL DEFAULT 1,
    auto_reply INTEGER NOT NULL DEFAULT 1,
    connected_at REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS agent_identities (
    agent_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'worker',
    created_at REAL,
    UNIQUE (user_id, slug)
);

CREATE TABLE IF NOT EXISTS email_drafts (
    draft_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_email_drafts_user ON email_drafts(user_id, created_at);

CREATE TABLE IF NOT EXISTS email_sends (
    email_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    to_alias TEXT NOT NULL DEFAULT '',
    recipient_email TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    gmail_message_id TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT NOT NULL DEFAULT '',
    gmail_thread_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'sending',
    idempotency_key TEXT NOT NULL UNIQUE,
    error_code TEXT NOT NULL DEFAULT '',
    created_at REAL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS idx_email_sends_user ON email_sends(user_id, created_at);

CREATE TABLE IF NOT EXISTS tracked_threads (
    thread_ref TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    gmail_thread_id TEXT NOT NULL,
    alias TEXT NOT NULL DEFAULT '',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tracked_threads_user ON tracked_threads(user_id);
CREATE INDEX IF NOT EXISTS idx_tracked_threads_gmail ON tracked_threads(gmail_thread_id);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    thread_ref TEXT NOT NULL DEFAULT '',
    processed_at REAL
);

CREATE TABLE IF NOT EXISTS routines (
    routine_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    task_prompt TEXT NOT NULL DEFAULT '',
    schedule_type TEXT NOT NULL,
    schedule_definition TEXT NOT NULL DEFAULT '{}',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at REAL,
    created_at REAL,
    updated_at REAL,
    last_run_at REAL,
    last_run_status TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_routines_owner ON routines(owner_id);
CREATE INDEX IF NOT EXISTS idx_routines_due ON routines(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS routine_runs (
    routine_run_id TEXT PRIMARY KEY,
    routine_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    scheduled_for REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    started_at REAL,
    completed_at REAL,
    result_summary TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    approval_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_routine_runs_owner ON routine_runs(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_routine_runs_lease ON routine_runs(status, lease_until);

CREATE TABLE IF NOT EXISTS computer_approvals (
    approval_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    execution_id TEXT NOT NULL,
    task_payload_hash TEXT NOT NULL,
    task TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    success_criteria TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'chat',
    routine_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL,
    expires_at REAL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_user ON computer_approvals(user_id, status);

CREATE TABLE IF NOT EXISTS delegated_jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    approval_id TEXT NOT NULL DEFAULT '',
    spec TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    lease_until REAL,
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_delegated_jobs_user ON delegated_jobs(user_id, status);

CREATE TABLE IF NOT EXISTS conversation_activity (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_activity_at REAL,
    summarized_through INTEGER NOT NULL DEFAULT 0,
    summary_status TEXT NOT NULL DEFAULT 'none',
    summary_due_at REAL
);
CREATE INDEX IF NOT EXISTS idx_conversation_activity_due ON conversation_activity(summary_status, summary_due_at);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    covered_from INTEGER NOT NULL DEFAULT 0,
    covered_to INTEGER NOT NULL DEFAULT 0,
    activity_version INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    created_at REAL,
    PRIMARY KEY (conversation_id, version)
);

CREATE TABLE IF NOT EXISTS legacy_quarantine (
    thread_id TEXT PRIMARY KEY,
    original_user_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    quarantined_at REAL
);

CREATE TABLE IF NOT EXISTS codespace_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend_url TEXT NOT NULL,
    registered_at REAL,
    expires_at REAL,
    checksum TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS durable_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    available_at REAL,
    created_at REAL,
    updated_at REAL,
    result TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_durable_jobs_due ON durable_jobs(status, available_at);

CREATE TABLE IF NOT EXISTS email_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    draft_id TEXT NOT NULL DEFAULT '',
    recipient_email TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_email_audit_user ON email_audit(user_id, created_at);
"""
