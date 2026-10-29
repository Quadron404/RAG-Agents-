/**
 * Worker-native mail_tool_call tool for Chief chat.
 *
 * The model can request an email to itself through Groq's native function call.
 * The actual sending uses the central Gmail sender account (GMAIL_SENDER_EMAIL
 * / GMAIL_SENDER_REFRESH_TOKEN as Cloudflare Worker secrets) -- users never
 * connect their personal Gmail just to receive notifications.
 *
 * A model tool call is only a request: the Worker creates a persistent,
 * owner-scoped, one-time pending approval in D1 and returns it without sending
 * anything.  The frontend shows an Allow/Deny card inline in chat; only after
 * the owner clicks Allow does the Worker send the email.
 *
 * The recipient is always the authenticated user's verified Google account email
 * -- the model cannot supply one.
 */
import type { Env } from "../env";
import type { ToolCall, ToolDefinition } from "./types";
import { createConnectorApproval } from "../connector-approvals";

export const MAIL_TOOL_CALL_TOOL: ToolDefinition = {
  name: "mail_tool_call",
  description:
    "Send an email to yourself (the signed-in user) through the central bot " +
    "mailbox. Supply only the message body; the recipient is fixed to your " +
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

export const MAIL_TOOL_CALL_NAME = MAIL_TOOL_CALL_TOOL.name;

export interface MailToolResult {
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

export function isMailToolCall(name: string): boolean {
  return name === MAIL_TOOL_CALL_NAME;
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/**
 * Execute mail_tool_call by creating a pending, owner-scoped, one-time approval
 * in Worker D1.  Nothing is sent until the owner clicks Allow on the card.
 * The email is then sent through the central Gmail sender, addressed to the
 * owner's verified email.
 */
export async function executeMailToolCall(
  env: Env,
  ownerSub: string,
  ownerId: string,
  call: ToolCall,
): Promise<MailToolResult> {
  if (call.name !== MAIL_TOOL_CALL_NAME) {
    return { ok: false, error: "The requested mail tool is not available." };
  }

  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(call.arguments || "{}");
  } catch {
    return { ok: false, error: "The mail tool arguments were malformed." };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: "The mail tool arguments were malformed." };
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
