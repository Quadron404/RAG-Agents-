/**
 * Worker-native connector tools for Chief chat.
 *
 * gmail.send_email creates a pending, owner-scoped, one-time approval in D1 --
 * no email is sent until the owner clicks Allow on the card.  The recipient is
 * always the authenticated user's verified Google account email; the model
 * cannot supply one.
 */
import type { Env } from "../env";
import type { ToolCall, ToolDefinition } from "./types";
import { createConnectorApproval, getConnectorApproval } from "../connector-approvals";

export const GMAIL_SEND_EMAIL_TOOL: ToolDefinition = {
  name: "gmail.send_email",
  description:
    "Send an email to yourself (the signed-in user) through the central bot " +
    "mailbox. Supply only the message body. The recipient is fixed to your " +
    "verified Google account email and cannot be changed. This is a request: the " +
    "owner must approve it on an Allow/Deny card before any email is sent.",
  parameters: {
    type: "object",
    properties: {
      content: {
        type: "string",
        description: "The email content to send to the user.",
      },
    },
    required: ["content"],
    additionalProperties: false,
  },
};

export const CONNECTOR_TOOLS: ToolDefinition[] = [GMAIL_SEND_EMAIL_TOOL];

const CONNECTOR_TOOL_NAMES = new Set(CONNECTOR_TOOLS.map((t) => t.name));

export interface ConnectorToolResult {
  ok: boolean;
  output: string;
  error?: string;
  approval?: {
    approval_id: string;
    connector: string;
    action: string;
    arguments: Record<string, unknown>;
    expires_at: number;
  };
}

export function isConnectorTool(name: string): boolean {
  return CONNECTOR_TOOL_NAMES.has(name);
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/**
 * Execute a connector tool by creating a pending, owner-scoped, one-time
 * approval in Worker D1.  Nothing is sent until the owner clicks Allow on the
 * card.  The email is then sent through the central Gmail sender.
 */
export async function executeConnectorTool(
  env: Env,
  ownerSub: string,
  ownerId: string,
  call: ToolCall,
): Promise<ConnectorToolResult> {
  if (!isConnectorTool(call.name)) {
    return { ok: false, output: "", error: "The requested connector tool is not available." };
  }

  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(call.arguments || "{}");
  } catch {
    return { ok: false, output: "", error: "The connector tool arguments were malformed." };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, output: "", error: "The connector tool arguments were malformed." };
  }
  const content = typeof parsed.content === "string" ? parsed.content.trim() : "";
  if (!content) {
    return { ok: false, output: "", error: "email content is required." };
  }

  if (!env.GMAIL_SENDER_EMAIL || !env.GMAIL_SENDER_REFRESH_TOKEN) {
    return {
      ok: false,
      output: "",
      error: "email is not configured on this server; contact your administrator",
    };
  }

  try {
    const approval = await createConnectorApproval(env, {
      user_id: ownerId,
      owner_sub: ownerSub,
      connector: "gmail",
      action: "send_email",
      arguments: { content, sender_email: env.GMAIL_SENDER_EMAIL },
    });
    return {
      ok: true,
      output:
        "Approval required: you will see an Allow/Deny card for sending this " +
        "email. No email is sent until you approve it.",
      approval: {
        approval_id: approval.approval_id,
        connector: "gmail",
        action: "send_email",
        arguments: { content, sender_email: env.GMAIL_SENDER_EMAIL },
        expires_at: approval.expires_at,
      },
    };
  } catch (error) {
    return { ok: false, output: "", error: `could not request connector approval: ${(error as Error).message}` };
  }
}

/**
 * Execute an already-approved connector action.
 *
 * Atomically transitions the approval from `pending` to `executing` before
 * sending.  Only the request that wins that transition may execute the action.
 * After sending, the approval is marked `allowed` with the Gmail message ID.
 * Deny transitions only a still-pending approval to `denied`.
 *
 * Concurrent requests, repeated clicks, retries, and stale approvals cannot
 * send the same email twice.
 */
export async function executeApprovedConnectorAction(
  env: Env,
  approvalId: string,
  decidedByUserId: string,
): Promise<{ ok: boolean; result?: unknown; error?: string }> {
  const now = Date.now();

  // Atomically claim: transition from pending to executing.  Only one request
  // can win this transition; a second request finds zero matching rows and
  // learns the approval is already decided or in progress.
  const claim = await env.DB.prepare(
    `UPDATE connector_approvals
       SET status = 'executing', updated_at = ?2
     WHERE approval_id = ?1 AND status = 'pending'`,
  )
    .bind(approvalId, now)
    .run();

  if (claim.meta?.changes === 0) {
    // The approval was not pending: already decided, already executing, or missing.
    const existing = await getConnectorApproval(env, approvalId);
    if (!existing) {
      return { ok: false, error: "no such approval" };
    }
    if (existing.user_id !== decidedByUserId) {
      return { ok: false, error: "approval does not belong to you" };
    }
    if (existing.status === "denied" || existing.status === "expired" || existing.status === "failed") {
      return { ok: false, error: `approval is ${existing.status}` };
    }
    if (existing.status === "executing" || existing.status === "allowed") {
      return { ok: false, error: `approval is already ${existing.status}` };
    }
    return { ok: false, error: `approval is ${existing.status}` };
  }

  const approval = await getConnectorApproval(env, approvalId);
  if (!approval) {
    return { ok: false, error: "no such approval" };
  }
  if (approval.user_id !== decidedByUserId) {
    // Should not happen because we just claimed it, but defend anyway.
    return { ok: false, error: "approval does not belong to you" };
  }

  if (approval.connector !== "gmail" || approval.action !== "send_email") {
    return { ok: false, error: "unknown connector action" };
  }

  const args = approval.arguments as { content?: string };
  const content = typeof args.content === "string" ? args.content : "";
  if (!content) {
    return { ok: false, error: "email content is missing from approval" };
  }

  const { sendGmail } = await import("../gmail-sender");
  try {
    // Resolve the owner's verified email address from D1.  The recipient is always
    // the authenticated user's verified Google email -- never from the model or
    // any tool argument.
    const { getUserById } = await import("../store");
    const user = await getUserById(env, approval.user_id);
    if (!user) {
      return { ok: false, error: "owner not found" };
    }
    const recipient = user.email;

    const messageId = await sendGmail(env, recipient, "RAG Agents", content);

    const resultJson = JSON.stringify({
      ok: true,
      connector: "gmail",
      action: "send_email",
      recipient,
      message_id: messageId,
    });

    // Mark the approval as allowed with the recorded result.
    await env.DB.prepare(
      `UPDATE connector_approvals
         SET status = 'allowed', decided_at = ?2, decided_by = ?3, result = ?4, updated_at = ?2
       WHERE approval_id = ?1 AND status = 'executing'`,
    )
      .bind(approvalId, now, decidedByUserId, resultJson)
      .run();

    // If this approval is linked to a routine run, mark that run completed too.
    if (approval.routine_run_id) {
      await env.DB.prepare(
        `UPDATE routine_runs
           SET status = 'completed', result_summary = ?1, completed_at = ?2, updated_at = ?2
         WHERE routine_run_id = ?3`,
      )
        .bind(JSON.stringify({ ok: true, connector: "gmail", action: "send_email", recipient, message_id: messageId }), now, approval.routine_run_id)
        .run();
      await env.DB.prepare(
        `UPDATE routines SET last_run_status = 'completed', updated_at = ?1 WHERE routine_id = ?2`,
      )
        .bind(now, approval.routine_id ?? "")
        .run();
    }

    return {
      ok: true,
      result: { ok: true, connector: "gmail", action: "send_email", recipient, message_id: messageId },
    };
  } catch (error) {
    const errMsg = (error as Error).message;
    // Mark the approval as failed so it cannot be replayed.
    await env.DB.prepare(
      `UPDATE connector_approvals
         SET status = 'failed', decided_at = ?2, decided_by = ?3, result = ?4, updated_at = ?2
       WHERE approval_id = ?1 AND status = 'executing'`,
    )
      .bind(approvalId, now, decidedByUserId, JSON.stringify({ ok: false, error: errMsg }))
      .run();
    return { ok: false, error: errMsg };
  }
}
