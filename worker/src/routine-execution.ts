/**
 * Worker-native routine execution using Groq GPT-OSS 20B.
 *
 * Runs a scheduled routine's task_prompt directly at the edge, without
 * depending on the Codespace backend.  The result is recorded in D1 by the
 * scheduler.
 */
import type { Env } from "../env";

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

interface GroqMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

interface GroqRequest {
  model: string;
  messages: GroqMessage[];
  temperature?: number;
  max_completion_tokens?: number;
}

interface GroqResponse {
  choices: Array<{ message: { content: string } }>;
  error?: { message: string };
}

/**
 * Execute one routine's task_prompt through Groq GPT-OSS 20B.
 * Returns the result text, or throws on failure.
 */
export async function executeRoutine(
  env: Env,
  taskPrompt: string,
): Promise<string> {
  const apiKey = env.GROQ_API_KEY;
  if (!apiKey) {
    throw new Error("Groq API key is not configured on this Worker");
  }

  const request: GroqRequest = {
    model: ROUTINE_MODEL,
    messages: [
      { role: "system", content: ROUTINE_SYSTEM_PROMPT },
      { role: "user", content: taskPrompt },
    ],
    temperature: 0.3,
    max_completion_tokens: 4000,
  };

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
  if (!choice || !choice.message.content) {
    throw new Error("Groq returned an empty result");
  }
  return choice.message.content;
}
