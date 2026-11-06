/**
 * Shared email service for all outbound email from the Worker.
 *
 * mail_tool_call and gmail.send_email both route through here so content
 * validation, approval lifecycle and sending live in one place.  The low-level
 * Gmail API call remains in gmail-sender.ts; this module owns policy, the
 * durable idempotency claim, and the email lifecycle states.
 *
 * Lifecycle states:
 *   drafted            - content prepared, not yet offered to the owner
 *   approval_pending   - offered on an Allow/Deny card, awaiting a decision
 *   approved           - owner clicked Allow; about to be sent
 *   sending            - Gmail API call in flight (single-flight, atomically claimed)
 *   accepted_by_gmail  - Gmail accepted the message (message ID recorded; NOT a delivery guarantee)
 *   failed             - Gmail rejected the message or a hard error occurred
 *   outcome_unknown    - Gmail's response was ambiguous; requires safe reconciliation
 */
import type { Env } from "../env";
import { createConnectorApproval, getConnectorApproval, resolveConnectorApproval, type ConnectorApproval } from "../connector-approvals";
import { sendGmail, GmailSenderError } from "../gmail-sender";
import { getUserById } from "../store";

export type EmailLifecycleState =
  | "drafted"
  | "approval_pending"
  | "approved"
  | "sending"
  | "accepted_by_gmail"
  | "failed"
  | "outcome_unknown"
  | "expired";

export interface EmailRequestInput {
  ownerSub: string;
  ownerId: string;
  content: string;
  senderEmail: string;
  subject?: string;
  routineId?: string;
  routineRunId?: string;
}

export interface EmailSendResult {
  ok: boolean;
  messageId?: string;
  error?: string;
  state: EmailLifecycleState;
}

/** Content safety limits. */
export const MAX_SUBJECT_CHARS = 200;
export const MAX_BODY_CHARS = 100_000;
export const DEFAULT_SUBJECT = "RAG Agents";

function isHeaderInjection(value: string): boolean {
  return /[\r\n]/.test(value);
}

/** Validate an email body: non-empty, within size, no header-injection newlines. */
export function validateEmailBody(body: string): { ok: true } | { ok: false; error: string } {
  if (!body || !body.trim()) {
    return { ok: false, error: "email content is required." };
  }
  if (body.length > MAX_BODY_CHARS) {
    return { ok: false, error: `email content is too long (${MAX_BODY_CHARS} characters maximum).` };
  }
  if (isHeaderInjection(body)) {
    return { ok: false, error: "email content must not contain newlines." };
  }
  return { ok: true };
}

/** Validate an email subject: within size, no header-injection newlines. */
export function validateEmailSubject(subject: string): { ok: true } | { ok: false; error: string } {
  if (!subject) return { ok: true };
  if (subject.length > MAX_SUBJECT_CHARS) {
    return { ok: false, error: "subject is too long (200 characters maximum)." };
  }
  if (isHeaderInjection(subject)) {
    return { ok: false, error: "subject must not contain newlines." };
  }
  return { ok: true };
}

/** Resolve the recipient from D1: always the owner's verified Google email. */
async function resolveRecipient(env: Env, ownerId: string): Promise<string | null> {
  const user = await getUserById(env, ownerId);
  return user?.email ?? null;
}

/** Create a pending, owner-scoped, one-time email approval. */
export async function createEmailApproval(
  env: Env,
  input: EmailRequestInput,
): Promise<{ ok: true; approval: ConnectorApproval } | { ok: false; error: string }> {
  if (!env.GMAIL_SENDER_EMAIL || !env.GMAIL_SENDER_REFRESH_TOKEN) {
    return { ok: false, error: "email is not configured on this server; contact your administrator" };
  }

  const subject = input.subject ?? DEFAULT_SUBJECT;
  const subjectCheck = validateEmailSubject(subject);
  if (!subjectCheck.ok) return { ok: false, error: subjectCheck.error };

  const bodyCheck = validateEmailBody(input.content);
  if (!bodyCheck.ok) return { ok: false, error: bodyCheck.error };

  const created = await createConnectorApproval(env, {
    user_id: input.ownerId,
    owner_sub: input.ownerSub,
    connector: "gmail",
    action: "send_email",
    arguments: { content: input.content, sender_email: input.senderEmail, subject },
    routine_id: input.routineId ?? undefined,
    routine_run_id: input.routineRunId ?? undefined,
  });

  return {
    ok: true,
    approval: {
      ...created,
      status: "approval_pending",
    } as ConnectorApproval,
  };
}

/**
 * Execute an already-approved email action.
 *
 * Atomically transitions the approval from `approval_pending` to `sending`
 * before calling the Gmail API.  Only the request that wins that transition may
 * send.  On success the approval becomes `accepted_by_gmail`; on a hard error
 * `failed`; on an ambiguous Gmail response `outcome_unknown`, which requires
 * safe reconciliation rather than a blind retry.
 */
export async function executeEmailApproval(
  env: Env,
  approvalId: string,
  decidedByUserId: string,
): Promise<EmailSendResult> {
  const now = Date.now();

  const existing = await getConnectorApproval(env, approvalId);
  if (!existing) {
    return { ok: false, state: "failed", error: "no such approval" };
  }
  if (existing.user_id !== decidedByUserId) {
    return { ok: false, state: "failed", error: "approval does not belong to you" };
  }
  if (existing.status !== "approval_pending") {
    return {
      ok: false,
      state: existing.status as EmailLifecycleState,
      error: `approval is ${existing.status}`,
    };
  }
  if (Date.now() > existing.expires_at) {
    await resolveConnectorApproval(env, approvalId, {
      decision: "expired",
      decidedBy: decidedByUserId,
      result: "expired before decision",
    });
    return { ok: false, state: "expired", error: "approval has expired" };
  }

  // Atomically claim: transition from approval_pending to sending.
  const claim = await env.DB.prepare(
    `UPDATE connector_approvals
       SET status = 'sending', updated_at = ?2
     WHERE approval_id = ?1 AND status = 'approval_pending'`,
  )
    .bind(approvalId, now)
    .run();

  if (claim.meta?.changes === 0) {
    const refreshed = await getConnectorApproval(env, approvalId);
    if (!refreshed) return { ok: false, state: "failed", error: "no such approval" };
    if (refreshed.user_id !== decidedByUserId) {
      return { ok: false, state: "failed", error: "approval does not belong to you" };
    }
    return {
      ok: false,
      state: refreshed.status as EmailLifecycleState,
      error: `approval is already ${refreshed.status}`,
    };
  }

  const args = existing.arguments as { content?: string; sender_email?: string; subject?: string };
  const content = typeof args.content === "string" ? args.content : "";
  if (!content) {
    await resolveConnectorApproval(env, approvalId, {
      decision: "failed",
      decidedBy: decidedByUserId,
      result: "email content is missing from approval",
    });
    return { ok: false, state: "failed", error: "email content is missing from approval" };
  }

  const recipient = await resolveRecipient(env, existing.user_id);
  if (!recipient) {
    await resolveConnectorApproval(env, approvalId, {
      decision: "failed",
      decidedBy: decidedByUserId,
      result: "owner not found",
    });
    return { ok: false, state: "failed", error: "owner not found" };
  }

  const subject = typeof args.subject === "string" && args.subject ? args.subject : DEFAULT_SUBJECT;
  const sender = args.sender_email ?? env.GMAIL_SENDER_EMAIL;

  try {
    const messageId = await sendGmail(env, recipient, subject, content);

    const resultJson = JSON.stringify({
      ok: true,
      connector: "gmail",
      action: "send_email",
      recipient,
      message_id: messageId,
    });

    await env.DB.prepare(
      `UPDATE connector_approvals
         SET status = 'accepted_by_gmail', decided_at = ?2, decided_by = ?3, result = ?4, updated_at = ?2
       WHERE approval_id = ?1 AND status = 'sending'`,
    )
      .bind(approvalId, now, decidedByUserId, resultJson)
      .run();

    if (existing.routine_run_id) {
      await env.DB.prepare(
        `UPDATE routine_runs
           SET status = 'completed', result_summary = ?1, completed_at = ?2, updated_at = ?2
         WHERE routine_run_id = ?3`,
      )
        .bind(JSON.stringify({ ok: true, connector: "gmail", action: "send_email", recipient, message_id: messageId }), now, existing.routine_run_id)
        .run();
      await env.DB.prepare(
        `UPDATE routines SET last_run_status = 'completed', updated_at = ?1 WHERE routine_id = ?2`,
      )
        .bind(now, existing.routine_id ?? "")
        .run();
    }

    return { ok: true, state: "accepted_by_gmail", messageId };
  } catch (error) {
    const errMsg = (error as Error).message;
    const ambiguous = isAmbiguousGmailError(error);
    const decision = ambiguous ? "outcome_unknown" : "failed";
    const result = ambiguous
      ? JSON.stringify({ ok: false, error: errMsg, state: "outcome_unknown", requires_reconciliation: true })
      : JSON.stringify({ ok: false, error: errMsg });

    // The claim already moved the approval out of 'approval_pending' into
    // 'sending', so resolveConnectorApproval (which requires 'approval_pending')
    // cannot be used here. Transition directly from 'sending' to the terminal
    // state so a retry can never resend the same email.
    await env.DB.prepare(
      `UPDATE connector_approvals
         SET status = ?2, decided_at = ?3, decided_by = ?4, result = ?5, updated_at = ?3
       WHERE approval_id = ?1 AND status = 'sending'`,
    )
      .bind(approvalId, decision, now, decidedByUserId, result)
      .run();

    return { ok: false, state: decision, error: errMsg };
  }
}

/** A Gmail error is ambiguous when Gmail may have accepted the message (5xx) or
 * returned no message id, so a blind retry could duplicate the send. */
function isAmbiguousGmailError(error: unknown): boolean {
  if (!(error instanceof GmailSenderError)) return false;
  const msg = error.message;
  return /Gmail API error [5][0-9][0-9]|no message id/i.test(msg);
}
