/**
 * Worker-native routine execution using Groq GPT-OSS 20B.
 *
 * Runs a scheduled routine's task_prompt directly at the edge, without
 * depending on the Codespace backend.  The result is recorded in D1 by the
 * scheduler.
 *
 * When a routine_run_id is supplied, Gmail connector tools are registered so
 * the model can request email sends.  Each request creates a pending,
 * owner-scoped approval linked to the routine run; nothing is sent until the
 * owner clicks Allow.
 */
import type { Env } from "./env";
import { createConnectorApproval } from "./connector-approvals";

const GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions";
const ROUTINE_MODEL = "openai/gpt-oss-20b";

const ROUTINE_SYSTEM_PROMPT =
  "You are executing a scheduled routine inside RAG Agents, an AI-agent platform " +
  "built to help users perform recurring tasks.\n\n" +
  "You are operating as a background task executor, not as the interactive Chief. " +
  "Execute the supplied routine instructions completely and systematically, using " +
  "only available information and authorized tools.\n\n" +
  "Follow the specified workflow. Do not fabricate research, results, tool " +
  "executions, or missing information.\n\n" +
  "You must respect the owner's permissions, the connector approval system, and " +
  "all backend security restrictions.\n\n" +
  "If an external connector is needed, describe the action you would take. " +
  "The backend will obtain the owner's explicit approval before executing it.\n\n" +
  "When completed, return a clear, structured result. If blocked or unsuccessful, " +
  "report the actual cause and any partial results.";

const GMAIL_SEND_EMAIL_TOOL = {
  name: "gmail.send_email",
  description:
    "Send an email to yourself (the signed-in user) through the central bot " +
    "mailbox. Supply only the message body. The recipient is fixed to your " +
    "verified Google account email and cannot be changed. This is a request: " +
    "the owner must approve it on an Allow/Deny card before any email is sent.",
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

interface GroqMessage {
  role: "system" | "user" | "assistant";
  content: string;
  tool_calls?: Array<{
    id: string;
    type: string;
    function: { name: string; arguments: string };
  }>;
}

interface GroqRequest {
  model: string;
  messages: GroqMessage[];
  tools?: Array<{ type: string; function: typeof GMAIL_SEND_EMAIL_TOOL }>;
  temperature?: number;
  max_completion_tokens?: number;
}

interface GroqChoice {
  message: GroqMessage;
  finish_reason?: string;
}

interface GroqResponse {
  choices: GroqChoice[];
  error?: { message: string };
}

export interface RoutineExecutionResult {
  text: string;
  approvalId?: string;
}

/**
 * Execute one routine's task_prompt through Groq GPT-OSS 20B.
 *
 * When `routineRunId` (with owner info) is provided, Gmail tools are
 * registered.  If the model calls gmail.send_email, a pending approval is
 * created linked to that run and returned in approvalId.  The run is not
 * considered complete until the owner resolves the approval.
 *
 * Returns the result text, optionally with an approval id.
 */
export async function executeRoutine(
  env: Env,
  taskPrompt: string,
  options?: {
    routineRunId?: string;
    ownerSub?: string;
    ownerId?: string;
  },
): Promise<RoutineExecutionResult> {
  const apiKey = env.GROQ_API_KEY;
  if (!apiKey) {
    throw new Error("Groq API key is not configured on this Worker");
  }

  const hasTools = Boolean(options?.routineRunId);
  const systemPrompt = hasTools
    ? ROUTINE_SYSTEM_PROMPT +
      "\n\nYou have access to a gmail.send_email tool. If the task instructions " +
      "involve sending email, use it. The recipient is always the " +
      "authenticated user's verified email -- you cannot change it. Each email " +
      "tool call creates a pending approval that the owner must explicitly " +
      "allow before any email is sent."
    : ROUTINE_SYSTEM_PROMPT;

  const request: GroqRequest = {
    model: ROUTINE_MODEL,
    messages: [
      { role: "system", content: systemPrompt },
      { role: "user", content: taskPrompt },
    ],
    temperature: 0.3,
    max_completion_tokens: 4000,
  };

  if (hasTools) {
    request.tools = [{ type: "function", function: GMAIL_SEND_EMAIL_TOOL }];
  }

  const response = await fetch(GROQ_CHAT_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(`Groq API error ${response.status}${errorText ? ": " + errorText.slice(0, 300) : ""}`);
  }

  const payload = (await response.json()) as GroqResponse;
  if (payload.error) {
    throw new Error(`Groq API error: ${payload.error.message}`);
  }
  const choice = payload.choices?.[0];
  if (!choice) {
    throw new Error("Groq returned an empty result");
  }

  const text = choice.message.content ?? "";

  if (hasTools && options?.routineRunId && choice.message.tool_calls) {
    const { routineRunId, ownerId = "", ownerSub = "" } = options!;
    for (const tc of choice.message.tool_calls) {
      if (tc.function.name === "gmail.send_email") {
        let parsed: Record<string, unknown>;
        try {
          parsed = JSON.parse(tc.function.arguments || "{}");
        } catch {
          continue;
        }
        const content =
          typeof parsed.content === "string" ? parsed.content.trim() : "";
        if (!content) continue;

        const approval = await createConnectorApproval(env, {
          user_id: ownerId,
          owner_sub: ownerSub,
          connector: "gmail",
          action: "send_email",
          arguments: {
            content,
            sender_email: env.GMAIL_SENDER_ALIAS ?? env.GMAIL_SENDER_EMAIL,
          },
          routine_run_id: routineRunId,
        });

        return { text, approvalId: approval.approval_id };
      }
    }
  }

  return { text };
}
