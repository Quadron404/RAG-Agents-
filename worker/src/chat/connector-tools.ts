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
import { createConnectorApproval, executeApprovedConnectorAction, getConnectorApproval } from "../connector-approvals";

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
    return { ok: false, error: "The requested connector tool is not available." };
  }

  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(call.arguments || "{}");
  } catch {
    return { ok: false, error: "The connector tool arguments were malformed." };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: "The connector tool arguments were malformed." };
  }
  const content = typeof parsed.content === "string" ? parsed.content.trim() : "";
  if (!content) {
    return { ok: false, error: "email content is required." };
  }

  if (!env.GMAIL_SENDER_EMAIL || !env.GMAIL_SENDER_REFRESH_TOKEN) {
    return {
      ok: false,
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
    return { ok: false, error: `could not request connector approval: ${(error as Error).message}` };
  }
}

/**
 * Execute an already-approved connector action.  Validates the approval
 * (owner, pending, not expired), sends the email through the central Gmail
 * sender, and records the result.
 */
export async function executeApprovedConnectorAction(
  env: Env,
  approvalId: string,
  decidedByUserId: string,
): Promise<{ ok: boolean; result?: unknown; error?: string }> {
  const approval = await getConnectorApproval(env, approvalId);
  if (!approval) {
    return { ok: false, error: "no such approval" };
  }
  if (approval.user_id !== decidedByUserId) {
    return { ok: false, error: "approval does not belong to you" };
  }
  if (approval.status !== "pending") {
    return { ok: false, error: `approval is ${approval.status}` };
  }
  if (Date.now() > approval.expires_at) {
    // expiry is enforced by resolveConnectorApproval too, but check early here
    return { ok: false, error: "approval has expired" };
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

    await env.DB.prepare(
      `UPDATE connector_approvals SET result = ?1, updated_at = ?2 WHERE approval_id = ?3`,
    )
      .bind(
        JSON.stringify({ ok: true, connector: "gmail", action: "send_email", recipient, message_id: messageId }),
        Date.now(),
        approvalId,
      )
      .run();

    return {
      ok: true,
      result: { ok: true, connector: "gmail", action: "send_email", recipient, message_id: messageId },
    };
  } catch (error) {
    return { ok: false, error: (error as Error).message };
  }
}
