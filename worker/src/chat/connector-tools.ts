/**
 * Worker-native connector tools for Chief chat.
 *
 * gmail.send_email sends directly through the central Gmail sender -- no
 * approval card.  The recipient is always the authenticated user's verified
 * Google account email; the model cannot supply one.
 */
import type { Env } from "../env";
import type { ToolCall, ToolDefinition } from "./types";
import { sendGmail } from "../gmail-sender";
import { getUserById } from "../store";

export const GMAIL_SEND_EMAIL_TOOL: ToolDefinition = {
  name: "gmail.send_email",
  description:
    "Send an email to yourself (the signed-in user) through the central bot " +
    "mailbox. Supply only the message body. The recipient is fixed to your " +
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

export const CONNECTOR_TOOLS: ToolDefinition[] = [GMAIL_SEND_EMAIL_TOOL];

const CONNECTOR_TOOL_NAMES = new Set(CONNECTOR_TOOLS.map((t) => t.name));

export interface ConnectorToolResult {
  ok: boolean;
  output: string;
  error?: string;
}

export function isConnectorTool(name: string): boolean {
  return CONNECTOR_TOOL_NAMES.has(name);
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/**
 * Execute a connector tool by sending directly through the central Gmail
 * sender, addressed to the owner's verified email.  No approval card -- the
 * send happens immediately when the tool result returns successfully.
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
