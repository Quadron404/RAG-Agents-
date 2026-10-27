/**
 * Worker-native mail_tool_call tool for Chief chat.
 *
 * The model can request an email to itself through Groq's native function call.
 * The actual sending uses the central Gmail sender account (GMAIL_SENDER_EMAIL
 * / GMAIL_SENDER_REFRESH_TOKEN as Cloudflare Worker secrets) -- users never
 * connect their personal Gmail just to receive notifications.
 *
 * The tool sends directly when called; no approval card is required.  The
 * recipient is always the authenticated user's verified Google account email --
 * the model cannot supply one.
 */
import type { Env } from "../env";
import type { ToolCall, ToolDefinition } from "./types";
import { sendGmail } from "../gmail-sender";
import { getUserById } from "../store";

export const MAIL_TOOL_CALL_TOOL: ToolDefinition = {
  name: "mail_tool_call",
  description:
    "Send an email to yourself (the signed-in user) through the central bot " +
    "mailbox. Supply only the message body; the recipient is fixed to your " +
    "verified Google account email and cannot be changed.",
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
}

export function isMailToolCall(name: string): boolean {
  return name === MAIL_TOOL_CALL_NAME;
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/**
 * Execute mail_tool_call by sending directly through the central Gmail sender,
 * addressed to the owner's verified email.  No approval card -- the send happens
 * immediately when the tool result returns successfully.
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

  // Resolve the owner's verified email address from D1.  The recipient is always
  // the authenticated user's verified Google email -- never from the model or
  // any tool argument.
  const user = await getUserById(env, ownerId);
  if (!user) {
    return { ok: false, error: "owner not found" };
  }
  const recipient = user.email;

  try {
    const messageId = await sendGmail(env, recipient, "RAG Agents", content);
    return {
      ok: true,
      output: `email sent to ${recipient}`,
      error: undefined,
    };
  } catch (error) {
    return { ok: false, error: (error as Error).message };
  }
}
