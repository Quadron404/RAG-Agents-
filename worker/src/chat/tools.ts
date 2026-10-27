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
  /** Whether the search ran successfully (true) even when it returned no matches. */
  searched: boolean;
}

/** Validation error codes surfaced to Chief without exposing internals. */
export const ToolError = {
  UNKNOWN_TOOL: "unknown_tool",
  EMPTY_QUERY: "empty_query",
  INVALID_ARGUMENTS: "invalid_arguments",
  SEARCH_FAILED: "search_failed",
} as const;

function queryFrom(call: ToolCall): { query: string; error?: string } {
  // Never execute a tool merely because plain assistant text claims to call it.
  // Tool calls arrive through the provider's native tool-call event type only.
  if (!call.name) {
    return { query: "", error: ToolError.INVALID_ARGUMENTS };
  }
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(call.arguments || "{}");
  } catch {
    return { query: "", error: ToolError.INVALID_ARGUMENTS };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { query: "", error: ToolError.INVALID_ARGUMENTS };
  }
  const query = parsed.query;
  if (typeof query !== "string" || !query.trim()) {
    return { query: "", error: ToolError.EMPTY_QUERY };
  }
  return { query: query.trim().slice(0, 500) };
}

export async function executeHistoryTool(
  env: Env,
  ownerSub: string,
  conversationId: string,
  call: ToolCall,
): Promise<ToolExecution> {
  // Validate the tool name strictly -- only the Worker-owned history tool runs.
  if (call.name !== HISTORY_TOOL.name) {
    return {
      ok: false,
      output: "The requested tool is not available.",
      resultCount: 0,
      error: ToolError.UNKNOWN_TOOL,
      searched: false,
    };
  }

  const parsed = queryFrom(call);
  if (parsed.error) {
    return {
      ok: false,
      output:
        parsed.error === ToolError.EMPTY_QUERY
          ? "The historical conversation search needs a non-empty query."
          : "The tool arguments were malformed.",
      resultCount: 0,
      error: parsed.error,
      searched: false,
    };
  }

  // Owner identity comes exclusively from the authenticated server-side session.
  // Model arguments never choose the owner or channel.
  const result = await searchConversationHistory(env, ownerSub, conversationId, parsed.query);
  if (!result.ok) {
    // A technical failure is never converted into a successful empty search.
    return {
      ok: false,
      output: result.error ?? "Historical conversation search failed.",
      resultCount: 0,
      error: ToolError.SEARCH_FAILED,
      searched: true,
    };
  }

  // Successful search with zero matches is distinct from a failed search.
  return {
    ok: true,
    output: result.text,
    resultCount: result.resultCount,
    searched: true,
  };
}
