-- Edge identity store for the stable workers.dev application.
--
-- This is the authority for "who is signed in": the Worker runs the Google
-- OpenID Connect flow, and every session the browser holds is a row here.  The
-- Codespace backend never sees the login flow; it trusts the Worker's identity
-- assertion, which is why sessions live at the edge and survive a backend
-- restart or a tunnel hostname change.

CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    google_sub     TEXT NOT NULL UNIQUE,
    email          TEXT NOT NULL DEFAULT '',
    email_verified INTEGER NOT NULL DEFAULT 0,
    name           TEXT NOT NULL DEFAULT '',
    picture        TEXT NOT NULL DEFAULT '',
    created_at     INTEGER,
    updated_at     INTEGER
);

-- Only the HMAC of a session token is stored.  A read of this table does not
-- yield a usable cookie, so a leaked database export cannot be replayed.
CREATE TABLE IF NOT EXISTS sessions (
    session_hash TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    csrf         TEXT NOT NULL,
    created_at   INTEGER,
    expires_at   INTEGER,
    revoked      INTEGER NOT NULL DEFAULT 0,
    rotated_from TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- PKCE verifier and nonce for a single in-flight authorization request.  The
-- state is consumed exactly once, which is what makes a replayed callback fail.
CREATE TABLE IF NOT EXISTS oauth_states (
    state         TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,
    code_verifier TEXT NOT NULL DEFAULT '',
    nonce         TEXT NOT NULL DEFAULT '',
    redirect_uri  TEXT NOT NULL DEFAULT '',
    next          TEXT NOT NULL DEFAULT '/',
    created_at    INTEGER,
    expires_at    INTEGER,
    consumed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub TEXT NOT NULL DEFAULT '',
    user_id    TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at INTEGER
);
