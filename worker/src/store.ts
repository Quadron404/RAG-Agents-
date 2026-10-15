/**
 * D1 accessors for the edge identity store.
 *
 * Users, sessions, in-flight authorization states and the login audit live
 * here.  A session token is only ever stored as an HMAC under SESSION_SECRET,
 * so a read of the `sessions` table yields no usable cookie.
 */
import type { Env } from "./env";
import { DEFAULT_TTL_SECONDS } from "./env";
import { hmacSha256Hex, randomToken } from "./encoding";

export interface UserRow {
  id: string;
  google_sub: string;
  email: string;
  email_verified: number;
  name: string;
  picture: string;
  created_at: number | null;
  updated_at: number | null;
}

export interface SessionRow {
  session_hash: string;
  user_id: string;
  csrf: string;
  created_at: number | null;
  expires_at: number | null;
  revoked: number;
  rotated_from: string;
}

export interface OAuthStateRow {
  state: string;
  mode: string;
  code_verifier: string;
  nonce: string;
  redirect_uri: string;
  next: string;
  created_at: number | null;
  expires_at: number | null;
  consumed: number;
}

function ttlSeconds(env: Env): number {
  const parsed = Number(env.SESSION_TTL_SECONDS);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_TTL_SECONDS;
}

// --- users ---------------------------------------------------------------

const USER_COLUMNS =
  "id, google_sub, email, email_verified, name, picture, created_at, updated_at";

export async function getUserById(env: Env, id: string): Promise<UserRow | null> {
  return env.DB.prepare(`SELECT ${USER_COLUMNS} FROM users WHERE id = ?1`)
    .bind(id)
    .first<UserRow>();
}

export async function getUserBySub(env: Env, googleSub: string): Promise<UserRow | null> {
  return env.DB.prepare(`SELECT ${USER_COLUMNS} FROM users WHERE google_sub = ?1`)
    .bind(googleSub)
    .first<UserRow>();
}

/**
 * Find or create the user for a verified Google subject.
 *
 * The subject is the identity; the email is a display attribute that Google may
 * change, so it is refreshed on every login but never used to look a user up.
 */
export async function upsertUser(
  env: Env,
  claims: { sub: string; email?: unknown; email_verified?: unknown; name?: unknown; picture?: unknown },
): Promise<UserRow> {
  const now = Date.now();
  const id = crypto.randomUUID();
  const row = await env.DB.prepare(
    `INSERT INTO users (id, google_sub, email, email_verified, name, picture, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?7)
     ON CONFLICT(google_sub) DO UPDATE SET
       email = excluded.email,
       email_verified = excluded.email_verified,
       name = excluded.name,
       picture = excluded.picture,
       updated_at = excluded.updated_at
     RETURNING ${USER_COLUMNS}`,
  )
    .bind(
      id,
      claims.sub,
      typeof claims.email === "string" ? claims.email : "",
      claims.email_verified === true ? 1 : 0,
      typeof claims.name === "string" ? claims.name : "",
      typeof claims.picture === "string" ? claims.picture : "",
      now,
    )
    .first<UserRow>();
  if (!row) {
    throw new Error("user upsert returned no row");
  }
  return row;
}

// --- sessions ------------------------------------------------------------

export interface CreatedSession {
  token: string;
  csrf: string;
  sessionHash: string;
  expiresAt: number;
}

export async function hashSessionToken(env: Env, token: string): Promise<string> {
  return hmacSha256Hex(env.SESSION_SECRET, token);
}

export async function createSession(
  env: Env,
  userId: string,
  rotatedFrom?: string,
): Promise<CreatedSession> {
  const token = randomToken(32);
  const csrf = randomToken(32);
  const sessionHash = await hashSessionToken(env, token);
  const now = Date.now();
  const expiresAt = now + ttlSeconds(env) * 1000;

  const statements = [
    env.DB.prepare(
      `INSERT INTO sessions (session_hash, user_id, csrf, created_at, expires_at, revoked, rotated_from)
       VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6)`,
    ).bind(sessionHash, userId, csrf, now, expiresAt, rotatedFrom ?? ""),
  ];
  if (rotatedFrom) {
    // Rotation replaces the session it was derived from in the same batch.
    statements.push(
      env.DB.prepare("UPDATE sessions SET revoked = 1 WHERE session_hash = ?1").bind(rotatedFrom),
    );
  }
  await env.DB.batch(statements);
  return { token, csrf, sessionHash, expiresAt };
}

export async function getValidSession(env: Env, token: string): Promise<SessionRow | null> {
  if (!token) return null;
  const hash = await hashSessionToken(env, token);
  return env.DB.prepare(
    `SELECT session_hash, user_id, csrf, created_at, expires_at, revoked, rotated_from
     FROM sessions WHERE session_hash = ?1 AND revoked = 0 AND expires_at > ?2`,
  )
    .bind(hash, Date.now())
    .first<SessionRow>();
}

export async function revokeSession(env: Env, token: string): Promise<boolean> {
  if (!token) return false;
  const hash = await hashSessionToken(env, token);
  const result = await env.DB.prepare("UPDATE sessions SET revoked = 1 WHERE session_hash = ?1")
    .bind(hash)
    .run();
  return (result.meta.changes ?? 0) > 0;
}

// --- authorization state -------------------------------------------------

export interface NewOAuthState {
  state: string;
  mode: string;
  codeVerifier: string;
  nonce: string;
  redirectUri: string;
  next?: string;
  ttlSeconds?: number;
}

export async function createOauthState(env: Env, value: NewOAuthState): Promise<void> {
  const now = Date.now();
  const expiresAt = now + (value.ttlSeconds ?? 600) * 1000;
  await env.DB.prepare(
    `INSERT INTO oauth_states (state, mode, code_verifier, nonce, redirect_uri, next, created_at, expires_at, consumed)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 0)`,
  )
    .bind(
      value.state,
      value.mode,
      value.codeVerifier,
      value.nonce,
      value.redirectUri,
      value.next ?? "/",
      now,
      expiresAt,
    )
    .run();
}

/**
 * Atomically read and invalidate an authorization state.
 *
 * The single UPDATE ... RETURNING makes consumption atomic, so two callbacks
 * racing on the same state cannot both succeed -- the second sees no row and is
 * treated as a replay.
 */
export async function consumeOauthState(
  env: Env,
  state: string,
): Promise<OAuthStateRow | null> {
  return env.DB.prepare(
    `UPDATE oauth_states SET consumed = 1
     WHERE state = ?1 AND consumed = 0 AND expires_at > ?2
     RETURNING state, mode, code_verifier, nonce, redirect_uri, next, created_at, expires_at, consumed`,
  )
    .bind(state, Date.now())
    .first<OAuthStateRow>();
}

// --- audit ---------------------------------------------------------------

export async function auditLogin(
  env: Env,
  googleSub: string,
  userId: string,
  outcome: string,
  reason = "",
): Promise<void> {
  await env.DB.prepare(
    `INSERT INTO login_audit (google_sub, user_id, outcome, reason, created_at)
     VALUES (?1, ?2, ?3, ?4, ?5)`,
  )
    .bind(googleSub, userId, outcome, reason, Date.now())
    .run();
}
