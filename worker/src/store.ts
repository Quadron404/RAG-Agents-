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
  timezone: string;
  developer_mode: number;
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
  "id, google_sub, email, email_verified, name, picture, timezone, developer_mode, created_at, updated_at";

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
    `INSERT INTO users (id, google_sub, email, email_verified, name, picture, timezone, developer_mode, created_at, updated_at)
      VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'UTC', 1, ?7, ?7)
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

/**
 * The owner's IANA timezone, validated against the IANA database.  Anything
 * unknown degrades to ``UTC``, the documented fallback.
 */
export async function getUserTimezone(env: Env, userId: string): Promise<string> {
  const row = await env.DB.prepare("SELECT timezone FROM users WHERE id = ?1")
    .bind(userId)
    .first<{ timezone: string }>();
  return row?.timezone ?? "UTC";
}

export async function setUserTimezone(
  env: Env,
  userId: string,
  timezone: string,
): Promise<boolean> {
  const result = await env.DB.prepare("UPDATE users SET timezone = ?2, updated_at = ?3 WHERE id = ?1")
    .bind(userId, timezone, Date.now())
    .run();
  return (result.meta.changes ?? 0) > 0;
}

/** 1 when Developer Mode is on (manual history, no automatic summaries). */
export async function getDeveloperMode(env: Env, userId: string): Promise<boolean> {
  const row = await env.DB.prepare("SELECT developer_mode FROM users WHERE id = ?1")
    .bind(userId)
    .first<{ developer_mode: number }>();
  return (row?.developer_mode ?? 0) === 1;
}

export async function setDeveloperMode(env: Env, userId: string, enabled: boolean): Promise<boolean> {
  const result = await env.DB.prepare(
    "UPDATE users SET developer_mode = ?2, updated_at = ?3 WHERE id = ?1",
  )
    .bind(userId, enabled ? 1 : 0, Date.now())
    .run();
  return (result.meta.changes ?? 0) > 0;
}

/* -------------------------------------------------------------------------- */
/* Agent identities, assignments, executions                                  */
/* -------------------------------------------------------------------------- */

export interface AgentIdentityRow {
  agent_id: string;
  owner_sub: string;
  owner_id: string;
  name: string;
  slug: string;
  role: string;
  description: string;
  specialization: string;
  capability_profile: string;
  identity_version: number;
  created_at: number;
  updated_at: number;
}

export interface AgentAssignmentRow {
  assignment_id: string;
  agent_id: string;
  owner_sub: string;
  owner_id: string;
  task_prompt: string;
  context: string;
  expected_deliverable: string;
  output_schema: string;
  status: string;
  created_at: number;
  updated_at: number;
}

export interface AgentExecutionRow {
  execution_id: string;
  assignment_id: string;
  agent_id: string;
  owner_sub: string;
  identity_version: number;
  status: string;
  result: string | null;
  error_code: string | null;
  started_at: number | null;
  completed_at: number | null;
  created_at: number;
}

const IDENTITY_COLUMNS =
  "agent_id, owner_sub, owner_id, name, slug, role, description, specialization, capability_profile, identity_version, created_at, updated_at";

const ASSIGNMENT_COLUMNS =
  "assignment_id, agent_id, owner_sub, owner_id, task_prompt, context, expected_deliverable, output_schema, status, created_at, updated_at";

const EXECUTION_COLUMNS =
  "execution_id, assignment_id, agent_id, owner_sub, identity_version, status, result, error_code, started_at, completed_at, created_at, updated_at";

export async function createAgentIdentity(
  env: Env,
  value: {
    agentId: string;
    ownerSub: string;
    ownerId: string;
    name: string;
    slug: string;
    role?: string;
    description?: string;
    specialization?: string;
    capabilityProfile?: Record<string, unknown>;
  },
): Promise<AgentIdentityRow> {
  const now = Date.now();
  const profile = JSON.stringify(value.capabilityProfile ?? {});
  const row = await env.DB.prepare(
    `INSERT INTO agent_identities
       (agent_id, owner_sub, owner_id, name, slug, role, description, specialization, capability_profile, identity_version, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 1, ?10, ?10)
     ON CONFLICT(agent_id) DO UPDATE SET
       name = excluded.name,
       slug = excluded.slug,
       role = excluded.role,
       description = excluded.description,
       specialization = excluded.specialization,
       capability_profile = excluded.capability_profile,
       updated_at = excluded.updated_at
     RETURNING ${IDENTITY_COLUMNS}`,
  )
    .bind(
      value.agentId,
      value.ownerSub,
      value.ownerId,
      value.name,
      value.slug,
      value.role ?? "specialist",
      value.description ?? "",
      value.specialization ?? "",
      profile,
      now,
    )
    .first<AgentIdentityRow>();
  if (!row) throw new Error("agent identity insert returned no row");
  return row;
}

export async function getAgentIdentity(
  env: Env,
  agentId: string,
): Promise<AgentIdentityRow | null> {
  return env.DB.prepare(
    `SELECT ${IDENTITY_COLUMNS} FROM agent_identities WHERE agent_id = ?1`,
  )
    .bind(agentId)
    .first<AgentIdentityRow>();
}

export async function getAgentIdentityBySlug(
  env: Env,
  ownerSub: string,
  slug: string,
): Promise<AgentIdentityRow | null> {
  return env.DB.prepare(
    `SELECT ${IDENTITY_COLUMNS} FROM agent_identities
     WHERE owner_sub = ?1 AND slug = ?2`,
  )
    .bind(ownerSub, slug)
    .first<AgentIdentityRow>();
}

export async function listAgentIdentities(
  env: Env,
  ownerSub: string,
): Promise<AgentIdentityRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${IDENTITY_COLUMNS} FROM agent_identities
     WHERE owner_sub = ?1
     ORDER BY updated_at DESC, agent_id ASC`,
  )
    .bind(ownerSub)
    .all<AgentIdentityRow>();
  return result.results ?? [];
}

export async function updateAgentIdentity(
  env: Env,
  agentId: string,
  value: {
    name?: string;
    description?: string;
    specialization?: string;
    capabilityProfile?: Record<string, unknown>;
  },
): Promise<AgentIdentityRow | null> {
  const now = Date.now();
  const sets: string[] = [];
  const args: unknown[] = [];
  let argIndex = 1;
  if (value.name !== undefined) {
    sets.push(`name = ?${argIndex++}`);
    args.push(value.name);
  }
  if (value.description !== undefined) {
    sets.push(`description = ?${argIndex++}`);
    args.push(value.description);
  }
  if (value.specialization !== undefined) {
    sets.push(`specialization = ?${argIndex++}`);
    args.push(value.specialization);
  }
  if (value.capabilityProfile !== undefined) {
    sets.push(`capability_profile = ?${argIndex++}`);
    args.push(JSON.stringify(value.capabilityProfile));
  }
  if (sets.length === 0) return getAgentIdentity(env, agentId);
  sets.push(`identity_version = identity_version + 1`);
  sets.push(`updated_at = ?${argIndex++}`);
  args.push(now);
  args.push(agentId);
  await env.DB.prepare(
    `UPDATE agent_identities SET ${sets.join(", ")} WHERE agent_id = ?${argIndex}`,
  )
    .bind(...args)
    .run();
  return getAgentIdentity(env, agentId);
}

export async function createAgentAssignment(
  env: Env,
  value: {
    assignmentId: string;
    agentId: string;
    ownerSub: string;
    ownerId: string;
    taskPrompt: string;
    context?: Record<string, unknown>;
    expectedDeliverable?: string;
    outputSchema?: Record<string, unknown>;
  },
): Promise<AgentAssignmentRow> {
  const now = Date.now();
  const row = await env.DB.prepare(
    `INSERT INTO agent_assignments
       (assignment_id, agent_id, owner_sub, owner_id, task_prompt, context, expected_deliverable, output_schema, status, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'pending', ?9, ?9)
     RETURNING ${ASSIGNMENT_COLUMNS}`,
  )
    .bind(
      value.assignmentId,
      value.agentId,
      value.ownerSub,
      value.ownerId,
      value.taskPrompt,
      JSON.stringify(value.context ?? {}),
      value.expectedDeliverable ?? "",
      JSON.stringify(value.outputSchema ?? {}),
      now,
    )
    .first<AgentAssignmentRow>();
  if (!row) throw new Error("agent assignment insert returned no row");
  return row;
}

export async function getAgentAssignment(
  env: Env,
  assignmentId: string,
): Promise<AgentAssignmentRow | null> {
  return env.DB.prepare(
    `SELECT ${ASSIGNMENT_COLUMNS} FROM agent_assignments WHERE assignment_id = ?1`,
  )
    .bind(assignmentId)
    .first<AgentAssignmentRow>();
}

export async function listAgentAssignments(
  env: Env,
  ownerSub: string,
  agentId?: string,
): Promise<AgentAssignmentRow[]> {
  let sql = `SELECT ${ASSIGNMENT_COLUMNS} FROM agent_assignments WHERE owner_sub = ?1`;
  const args: unknown[] = [ownerSub];
  if (agentId) {
    sql += ` AND agent_id = ?2`;
    args.push(agentId);
  }
  sql += ` ORDER BY created_at DESC`;
  const result = await env.DB.prepare(sql)
    .bind(...args)
    .all<AgentAssignmentRow>();
  return result.results ?? [];
}

export async function updateAgentAssignmentStatus(
  env: Env,
  assignmentId: string,
  status: string,
): Promise<boolean> {
  const now = Date.now();
  const result = await env.DB.prepare(
    "UPDATE agent_assignments SET status = ?2, updated_at = ?3 WHERE assignment_id = ?1",
  )
    .bind(assignmentId, status, now)
    .run();
  return (result.meta.changes ?? 0) > 0;
}

export async function createAgentExecution(
  env: Env,
  value: {
    executionId: string;
    assignmentId: string;
    agentId: string;
    ownerSub: string;
    identityVersion: number;
  },
): Promise<AgentExecutionRow> {
  const now = Date.now();
  const row = await env.DB.prepare(
    `INSERT INTO agent_executions
       (execution_id, assignment_id, agent_id, owner_sub, identity_version, status, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, 'pending', ?6, ?6)
     RETURNING ${EXECUTION_COLUMNS}`,
  )
    .bind(
      value.executionId,
      value.assignmentId,
      value.agentId,
      value.ownerSub,
      value.identityVersion,
      now,
    )
    .first<AgentExecutionRow>();
  if (!row) throw new Error("agent execution insert returned no row");
  return row;
}

export async function finishAgentExecution(
  env: Env,
  executionId: string,
  value: {
    status: string;
    result?: string | null;
    errorCode?: string | null;
  },
): Promise<boolean> {
  const now = Date.now();
  const result = await env.DB.prepare(
    `UPDATE agent_executions
       SET status = ?2, result = COALESCE(?3, result), error_code = COALESCE(?4, error_code),
           completed_at = ?5, updated_at = ?5
     WHERE execution_id = ?1`,
  )
    .bind(executionId, value.status, value.result ?? null, value.errorCode ?? null, now)
    .run();
  return (result.meta.changes ?? 0) > 0;
}

export async function getAgentExecution(
  env: Env,
  executionId: string,
): Promise<AgentExecutionRow | null> {
  return env.DB.prepare(
    `SELECT ${EXECUTION_COLUMNS} FROM agent_executions WHERE execution_id = ?1`,
  )
    .bind(executionId)
    .first<AgentExecutionRow>();
}

export async function listAgentExecutions(
  env: Env,
  ownerSub: string,
  assignmentId?: string,
): Promise<AgentExecutionRow[]> {
  let sql = `SELECT ${EXECUTION_COLUMNS} FROM agent_executions WHERE owner_sub = ?1`;
  const args: unknown[] = [ownerSub];
  if (assignmentId) {
    sql += ` AND assignment_id = ?2`;
    args.push(assignmentId);
  }
  sql += ` ORDER BY created_at DESC`;
  const result = await env.DB.prepare(sql)
    .bind(...args)
    .all<AgentExecutionRow>();
  return result.results ?? [];
}

