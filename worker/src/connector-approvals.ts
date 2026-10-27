/**
 * Worker-native connector approvals stored in D1.
 *
 * A model tool call is only a request: the Worker creates a pending,
 * owner-scoped, one-time approval here.  The UI shows the Allow/Deny card.
 * Only after the owner clicks Allow does the Worker execute the action.
 * Expired or already-decided approvals can never be replayed.
 */
import type { Env } from "../env";

export interface ConnectorApproval {
  approval_id: string;
  user_id: string;
  owner_sub: string;
  connector: string;
  action: string;
  arguments: Record<string, unknown>;
  status: string;
  created_at: number;
  expires_at: number;
  decided_at: number | null;
  decided_by: string | null;
  result: string | null;
  routine_id: string | null;
  routine_run_id: string | null;
}

const APPROVAL_COLUMNS =
  "approval_id, user_id, owner_sub, connector, action, arguments, status, " +
  "created_at, expires_at, decided_at, decided_by, result, routine_id, routine_run_id";

const APPROVAL_TTL_SECONDS = 30 * 60; // 30 minutes

function nowMs(): number {
  return Date.now();
}

export async function createConnectorApproval(
  env: Env,
  input: {
    user_id: string;
    owner_sub: string;
    connector: string;
    action: string;
    arguments: Record<string, unknown>;
    routine_id?: string;
    routine_run_id?: string;
  },
): Promise<ConnectorApproval> {
  const approval_id = crypto.randomUUID();
  const created_at = nowMs();
  const expires_at = created_at + APPROVAL_TTL_SECONDS * 1000;

  await env.DB.prepare(
    `INSERT INTO connector_approvals
       (approval_id, user_id, owner_sub, connector, action, arguments, status,
        created_at, expires_at, routine_id, routine_run_id)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'pending', ?7, ?8, ?9, ?10)`,
  )
    .bind(
      approval_id,
      input.user_id,
      input.owner_sub,
      input.connector,
      input.action,
      JSON.stringify(input.arguments),
      created_at,
      expires_at,
      input.routine_id ?? "",
      input.routine_run_id ?? "",
    )
    .run();

  return {
    approval_id,
    user_id: input.user_id,
    owner_sub: input.owner_sub,
    connector: input.connector,
    action: input.action,
    arguments: input.arguments,
    status: "pending",
    created_at,
    expires_at,
    decided_at: null,
    decided_by: null,
    result: null,
    routine_id: input.routine_id ?? null,
    routine_run_id: input.routine_run_id ?? null,
  };
}

export async function getConnectorApproval(
  env: Env,
  approvalId: string,
): Promise<ConnectorApproval | null> {
  const row = await env.DB.prepare(
    `SELECT ${APPROVAL_COLUMNS} FROM connector_approvals WHERE approval_id = ?1`,
  )
    .bind(approvalId)
    .first<ConnectorApproval & { arguments: string }>();
  if (!row) return null;
  return { ...row, arguments: JSON.parse(row.arguments) };
}

export async function listPendingApprovals(
  env: Env,
  userId: string,
  includeExpired = false,
): Promise<ConnectorApproval[]> {
  const now = nowMs();
  let rows;
  if (includeExpired) {
    const result = await env.DB.prepare(
      `SELECT ${APPROVAL_COLUMNS} FROM connector_approvals
       WHERE user_id = ?1 AND status = 'pending'
       ORDER BY created_at DESC`,
    )
      .bind(userId)
      .all<ConnectorApproval & { arguments: string }>();
    rows = result.results;
  } else {
    const result = await env.DB.prepare(
      `SELECT ${APPROVAL_COLUMNS} FROM connector_approvals
       WHERE user_id = ?1 AND status = 'pending' AND expires_at > ?2
       ORDER BY created_at DESC`,
    )
      .bind(userId, now)
      .all<ConnectorApproval & { arguments: string }>();
    rows = result.results;
  }
  return rows.map((r) => ({ ...r, arguments: JSON.parse(r.arguments) }));
}

export interface ResolveApprovalInput {
  decision: "allowed" | "denied" | "expired" | "failed";
  decidedBy: string;
  result?: string;
}

export async function resolveConnectorApproval(
  env: Env,
  approvalId: string,
  input: ResolveApprovalInput,
): Promise<ConnectorApproval | null> {
  const row = await env.DB.prepare(
    `SELECT ${APPROVAL_COLUMNS} FROM connector_approvals
     WHERE approval_id = ?1 AND status = 'pending'`,
  )
    .bind(approvalId)
    .first<ConnectorApproval & { arguments: string }>();
  if (!row) return null;

  const now = nowMs();
  const expired = now > row.expires_at;
  if (expired) {
    input.decision = "expired";
    input.result = input.result ?? "expired before decision";
  }

  await env.DB.prepare(
    `UPDATE connector_approvals
       SET status = ?2, decided_at = ?3, decided_by = ?4, result = ?5, updated_at = ?3
     WHERE approval_id = ?1`,
  )
    .bind(
      approvalId,
      input.decision,
      now,
      input.decidedBy,
      input.result ?? "",
    )
    .run();

  return {
    ...row,
    status: input.decision,
    decided_at: now,
    decided_by: input.decidedBy,
    result: input.result ?? null,
  };
}

export async function expireConnectorApprovals(env: Env, userId: string): Promise<number> {
  const now = nowMs();
  const result = await env.DB.prepare(
    `UPDATE connector_approvals
       SET status = 'expired', decided_at = ?2, decided_by = 'system', result = 'expired before decision', updated_at = ?2
     WHERE user_id = ?1 AND status = 'pending' AND expires_at <= ?3`,
  )
    .bind(userId, now, now)
    .run();
  return result.meta?.changes ?? 0;
}
