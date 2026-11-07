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
import { parseEmailContent, requestEmailSendApproval } from "./connector-tools";

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
    return { ok: false, output: "", error: "The requested mail tool is not available." };
  }
  const parsed = parseEmailContent(call);
  if (!parsed.ok) {
    return { ok: false, output: "", error: parsed.error };
  }
  const result = await requestEmailSendApproval(env, ownerSub, ownerId, parsed.content);
  if (!result.ok) {
    return { ok: false, output: "", error: result.error };
  }
  return {
    ok: true,
    output: result.output,
    approval: result.approval,
  };
}
