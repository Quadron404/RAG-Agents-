/** The one Worker-owned chat tool: search earlier conversations by summary. */
import type { Env } from "../env";
import { searchConversationHistory } from "./search";
import type { ToolCall, ToolDefinition } from "./types";

export const HISTORY_TOOL: ToolDefinition = {
  name: "tool_call_history",
  description:
    "Search summaries of this user's earlier conversations. Use it only when the current conversation lacks information needed to answer, and use the returned evidence rather than guessing.",
  parameters: {
    type: "object",
    properties: {
      query: {
        type: "string",
        description:
          "A precise search query describing the information needed, including subjects, technical terms, decisions, or dates when known.",
      },
    },
    required: ["query"],
    additionalProperties: false,
  },
};

export interface ToolExecution {
  ok: boolean;
  output: string;
  resultCount: number;
  error?: string;
}

function queryFrom(call: ToolCall): string {
  try {
    const parsed = JSON.parse(call.arguments || "{}") as Record<string, unknown>;
    const query = parsed.query;
    return typeof query === "string" ? query.slice(0, 500) : "";
  } catch {
    return "";
  }
}

export async function executeHistoryTool(
  env: Env,
  ownerSub: string,
  call: ToolCall,
): Promise<ToolExecution> {
  if (call.name !== HISTORY_TOOL.name) {
    return {
      ok: false,
      output: "The requested tool is not available.",
      resultCount: 0,
      error: "unknown_tool",
    };
  }
  const query = queryFrom(call);
  if (!query.trim()) {
    return {
      ok: false,
      output: "The historical conversation search needs a non-empty query.",
      resultCount: 0,
      error: "empty_query",
    };
  }
  const result = await searchConversationHistory(env, ownerSub, query);
  if (!result.ok) {
    return {
      ok: false,
      output: result.error ?? "Historical conversation search failed.",
      resultCount: 0,
      error: "search_failed",
    };
  }
  return { ok: true, output: result.text, resultCount: result.resultCount };
}
