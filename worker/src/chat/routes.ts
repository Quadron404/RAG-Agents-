/**
 * Worker-native conversation API and streaming chat turn.
 *
 * The browser no longer has a chat WebSocket: normal API calls create, list,
 * rename, and delete conversations, while POST /threads/:id/messages streams
 * Server-Sent Events.  All tenancy comes from the edge session; the client's
 * user id is never trusted.
 */
import type { Env } from "../env";
import { CSRF_HEADER, SAFE_METHODS } from "../env";
import { timingSafeEqual } from "../encoding";
import { json } from "../http";
import { resolvePrincipal, type Principal } from "../principal";
import { localDay } from "../timezone";
import { selectChatProvider, providerStatus } from "./providers";
import {
  autoTitle,
  acquireConversationLock,
  countUserMessages,
  createConversation,
  createRequest,
  deleteConversation,
  getConversation,
  getMessageById,
  getRequest,
  insertHiddenToolCall,
  insertHiddenToolResult,
  insertMessage,
  lastVisibleSeq,
  listConversations,
  listMessages,
  mapConversation,
  mapMessage,
  markRequest,
  newId,
  nowMs,
  releaseConversationLock,
  renameConversation,
  resetRequestForRetry,
  setTitleIfDefault,
  touchConversation,
  updateMessage,
  type ConversationRow,
  type MessageRow,
} from "./store";
import { generateIncrementalHistory } from "./history";
import { removeSummaryDoc, searchConfigured } from "./search";
import { executeHistoryTool, HISTORY_TOOL } from "./tools";
import {
  executeRoutineTool,
  isRoutineTool,
  ROUTINE_TOOLS,
} from "./routine-tools";
import {
  executeMailToolCall,
  isMailToolCall,
  MAIL_TOOL_CALL_TOOL,
} from "./mail-tool";
import {
  executeConnectorTool,
  isConnectorTool,
  CONNECTOR_TOOLS,
} from "./connector-tools";
import {
  ProviderError,
  type LlmMessage,
  type ProviderSelection,
  type ToolCall,
} from "./types";

export const FIRST_TURN_INSTRUCTION =
  "You are Chief, RAG Agents' central AI commander. Introduce yourself to the user and guide other AI agents in completing tasks.\n\n" +
  "## Routines (create_routine, list_routines, get_routine, pause_routine, resume_routine, delete_routine)\n" +
  "When the user asks you to do something repeatedly, set up a routine:\n" +
  "1. Identify the recurring task and intended result.\n" +
  "2. Write detailed, self-contained instructions for each run covering: OBJECTIVE, INPUTS, WORKFLOW, VERIFICATION, OUTPUT, CONSTRAINTS, FAILURE HANDLING.\n" +
  "3. Ask for the schedule if it is not stated: how often (daily/monthly), at what time (HH:MM), and for monthly routines which day of month (1-28).\n" +
  "4. Call create_routine with a name, the full task_prompt, schedule_type, time, and optionally day_of_month and timezone.\n" +
  "5. Return the actual creation result so the user sees the routine card.\n" +
  "Do not invent missing scheduling information; ask the user when needed.\n\n" +
  "## History retrieval (tool_call_history)\n" +
  "When the user's answer depends on earlier preferences, decisions, configurations, errors, plans, rejected options, or other historical facts that are not already established in the current conversation, you MUST call tool_call_history with a precise query before answering.\n" +
  "Examples:\n" +
  "- \"What do I like?\" → retrieve historical preferences.\n" +
  "- \"Which provider did we reject last month?\" → retrieve past decisions.\n" +
  "- \"What error did we see with the database yesterday?\" → retrieve past errors.\n" +
  "Do NOT call history for ordinary general-knowledge questions or when the current conversation already provides sufficient evidence.\n" +
  "Use the actual returned evidence. Distinguish confirmed decisions from proposals. Acknowledge missing or contradictory evidence without inventing facts.\n" +
  "Synthesize a natural, concise answer from the evidence. Do NOT dump raw search results, internal JSON, " +
  "timestamps, or unrelated metadata into your response.\n" +
  "Never treat a newer timestamp as proof that an earlier decision was replaced.\n\n" +
  "## Email (mail_tool_call)\n" +
  "When the user asks you to send an email, call mail_tool_call with the message body. " +
  "The recipient is fixed to your verified Google account email and cannot be changed. " +
  "Never claim the email was sent unless the tool result confirms it."

const MAX_CONTENT_CHARS = 100_000;
const MAX_TOOL_ITERATIONS = 8;

const REFUSAL_PATTERNS = [
  /i\s+(can|cannot|can't|will not|won't|do not|don't)\s+(send|email|mail)/i,
  /i\s+(am\s+)?(not\s+)?(able|unable)\s+to\s+(send|email|mail)/i,
  /i\s+(apologize|apologise|sorry)/i,
  /i\s+refuse/i,
  /i\s+decline/i,
  /cannot\s+send\s+email/i,
  /unable\s+to\s+send/i,
];

function isRefusal(text: string): boolean {
  return REFUSAL_PATTERNS.some((re) => re.test(text.trim()));
}

export function isChatPath(path: string): boolean {
  return path === "/threads" || path.startsWith("/threads/") || path === "/chat/status";
}

function requireCsrf(request: Request, principal: Principal): Response | null {
  const supplied = request.headers.get(CSRF_HEADER) ?? "";
  const expected = principal.session.csrf ?? "";
  if (!expected || !timingSafeEqual(supplied, expected)) {
    return json({ error: "invalid CSRF token" }, 403);
  }
  return null;
}

function parts(path: string): string[] {
  return path.split("/").filter(Boolean);
}

function intFrom(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : fallback;
}

function createConversationTitle(title: unknown): string {
  const text = typeof title === "string" ? title.trim() : "";
  return (text || "New chat").slice(0, 160);
}

function requestIdFrom(value: unknown): string | null {
  if (value === undefined || value === null || value === "") return null;
  const text = String(value);
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(text)) return null;
  return text;
}

function parseData(value: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(value || "{}");
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function toolCallsFromData(data: Record<string, unknown>): ToolCall[] {
  const raw = data.tool_calls;
  if (!Array.isArray(raw)) return [];
  const calls: ToolCall[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const item = entry as Record<string, unknown>;
    const id = typeof item.id === "string" ? item.id : "";
    const name = typeof item.name === "string" ? item.name : "";
    const args = typeof item.arguments === "string" ? item.arguments : "{}";
    if (name) calls.push({ id: id || `call_${calls.length}`, name, arguments: args });
  }
  return calls;
}

export function llmMessagesFrom(
  rows: MessageRow[],
  skipId: string,
  includeSystem = true,
): LlmMessage[] {
  const out: LlmMessage[] = [];
  for (const row of rows) {
    if (row.id === skipId) continue;
    if (row.role === "system") {
      if (includeSystem && row.content.trim()) out.push({ role: "system", content: row.content });
      continue;
    }
    if (row.role === "user") {
      out.push({ role: "user", content: row.content });
      continue;
    }
    if (row.role === "assistant") {
      const data = parseData(row.data);
      const calls = toolCallsFromData(data);
      if (row.hidden && !calls.length) continue;
      if (!row.content.trim() && !calls.length) continue;
      out.push({ role: "assistant", content: row.content, toolCalls: calls.length ? calls : undefined });
      continue;
    }
    if (row.role === "tool") {
      const data = parseData(row.data);
      out.push({
        role: "tool",
        content: row.content,
        toolCallId: typeof data.tool_call_id === "string" ? data.tool_call_id : "",
        name: typeof data.name === "string" ? data.name : undefined,
      });
    }
  }
  return out;
}

/* -------------------------------------------------------------------------- */
/* SSE                                                                         */
/* -------------------------------------------------------------------------- */

const encoder = new TextEncoder();

function sse(type: string, data: unknown): Uint8Array {
  return encoder.encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`);
}

function sseHeaders(): HeadersInit {
  return {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  };
}

interface Emitter {
  send(type: string, data: unknown): void;
  close(): void;
}

function streamResponse(
  request: Request,
  run: (emit: Emitter, signal: AbortSignal) => Promise<void>,
): Response {
  const abort = new AbortController();
  const onAbort = () => abort.abort(request.signal.reason);
  if (request.signal.aborted) abort.abort(request.signal.reason);
  else request.signal.addEventListener("abort", onAbort, { once: true });

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      let closed = false;
      const emit: Emitter = {
        send(type, data) {
          if (closed) return;
          try {
            controller.enqueue(sse(type, data));
          } catch {
            closed = true;
          }
        },
        close() {
          if (closed) return;
          closed = true;
          try {
            controller.close();
          } catch {
            /* client already disconnected */
          }
        },
      };
      void run(emit, abort.signal)
        .catch(() => {
          emit.send("error", { error: "The response stream failed." });
        })
        .finally(() => {
          request.signal.removeEventListener("abort", onAbort);
          emit.close();
        });
    },
    cancel() {
      abort.abort(new Error("client disconnected"));
    },
  });
  return new Response(stream, { headers: sseHeaders() });
}

/* -------------------------------------------------------------------------- */
/* Route handlers                                                              */
/* -------------------------------------------------------------------------- */

async function handleList(env: Env, principal: Principal): Promise<Response> {
  const rows = await listConversations(env, principal.user.google_sub);
  return json({ threads: rows.map(mapConversation) });
}

async function handleCreate(
  env: Env,
  principal: Principal,
  request: Request,
): Promise<Response> {
  const body = await readJson(request);
  const row = await createConversation(
    env,
    principal.user.google_sub,
    principal.user.id,
    createConversationTitle(body?.title),
  );
  return json(mapConversation(row), 201);
}

async function handleRename(
  env: Env,
  principal: Principal,
  conversationId: string,
  request: Request,
): Promise<Response> {
  const body = await readJson(request);
  const title = createConversationTitle(body?.title);
  const row = await renameConversation(env, principal.user.google_sub, conversationId, title);
  if (!row) return json({ error: "conversation not found" }, 404);
  return json(mapConversation(row));
}

async function handleDelete(
  env: Env,
  principal: Principal,
  conversationId: string,
): Promise<Response> {
  const removed = await deleteConversation(env, principal.user.google_sub, conversationId);
  if (!removed) return json({ error: "conversation not found" }, 404);
  await removeSummaryDoc(env, principal.user.google_sub, conversationId);
  return json({ ok: true });
}

async function handleMessages(
  env: Env,
  principal: Principal,
  conversationId: string,
): Promise<Response> {
  const conversation = await getConversation(env, principal.user.google_sub, conversationId);
  if (!conversation) return json({ error: "conversation not found" }, 404);
  const rows = await listMessages(env, principal.user.google_sub, conversationId, {
    visibleOnly: true,
  });
  return json({ messages: rows.map(mapMessage) });
}

async function readJson(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const value = await request.json();
    return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/* -------------------------------------------------------------------------- */
/* Chat turn                                                                   */
/* -------------------------------------------------------------------------- */

interface TurnContext {
  env: Env;
  principal: Principal;
  conversation: ConversationRow;
  requestId: string;
  userMessage: MessageRow;
  assistantMessage: MessageRow;
  provider: ProviderSelection;
  firstTurn: boolean;
}

async function ensureMessages(
  env: Env,
  principal: Principal,
  conversation: ConversationRow,
  requestId: string,
  userId: string,
  assistantId: string,
  content: string,
): Promise<{ userMessage: MessageRow; assistantMessage: MessageRow; firstTurn: boolean }> {
  let userMessage = await getMessageById(env, principal.user.google_sub, userId);
  const existingUsers = await countUserMessages(env, conversation.id);
  const firstTurn = existingUsers <= (userMessage ? 1 : 0);
  if (!userMessage) {
    if (firstTurn) {
      await insertMessage(env, {
        conversationId: conversation.id,
        ownerSub: principal.user.google_sub,
        role: "system",
        agent: "chief",
        content: FIRST_TURN_INSTRUCTION,
        hidden: true,
      });
    }
    userMessage = await insertMessage(env, {
      id: userId,
      conversationId: conversation.id,
      ownerSub: principal.user.google_sub,
      role: "user",
      agent: "user",
      content,
      requestId,
    });
  }
  let assistantMessage = await getMessageById(env, principal.user.google_sub, assistantId);
  if (!assistantMessage) {
    assistantMessage = await insertMessage(env, {
      id: assistantId,
      conversationId: conversation.id,
      ownerSub: principal.user.google_sub,
      role: "assistant",
      agent: "chief",
      content: "",
      requestId,
    });
  }
  return { userMessage, assistantMessage, firstTurn };
}

async function startTurn(
  request: Request,
  env: Env,
  principal: Principal,
  conversation: ConversationRow,
): Promise<Response> {
  const body = await readJson(request);
  const content = typeof body?.content === "string" ? body.content : "";
  if (!content.trim()) return json({ error: "a message is required" }, 400);
  if (content.length > MAX_CONTENT_CHARS) {
    return json({ error: "the message is too long" }, 413);
  }
  const provided = requestIdFrom(body?.request_id ?? body?.client_request_id);
  if ((body?.request_id ?? body?.client_request_id) !== undefined && !provided) {
    return json({ error: "invalid request id" }, 400);
  }
  const requestId = provided ?? `req-${newId()}`;
  const now = nowMs();

  const existing = await getRequest(env, requestId);
  if (existing) {
    if (existing.owner_sub !== principal.user.google_sub || existing.conversation_id !== conversation.id) {
      return json({ error: "request id already belongs to another conversation" }, 409);
    }
    if (existing.status === "streaming") {
      return json({ error: "this request is already in progress", request_id: requestId }, 409);
    }
    if (existing.status === "completed") {
      const assistant = await getMessageById(env, principal.user.google_sub, existing.assistant_message_id);
      const user = await getMessageById(env, principal.user.google_sub, existing.user_message_id);
      return streamResponse(request, async (emit) => {
        emit.send("meta", {
          request_id: requestId,
          conversation_id: conversation.id,
          user_message_id: existing.user_message_id,
          assistant_message_id: existing.assistant_message_id,
          user_message: user ? mapMessage(user) : null,
          model: "",
          provider: "",
          replay: true,
        });
        if (assistant) emit.send("done", { request_id: requestId, message: mapMessage(assistant) });
        else emit.send("error", { request_id: requestId, error: "the stored answer is missing" });
      });
    }
  }

  const provider = selectChatProvider(env);
  if (!provider) {
    return json(
      {
        error:
          "No AI provider is configured on the Worker. Add a provider API key and deploy again.",
      },
      503,
    );
  }

  const locked = await acquireConversationLock(env, principal.user.google_sub, conversation.id, requestId, now);
  if (!locked) {
    return json({ error: "this conversation is already generating a response" }, 409);
  }

  try {
    let requestRow = existing;
    let userMessageId: string;
    let assistantMessageId: string;

    if (requestRow) {
      userMessageId = requestRow.user_message_id;
      assistantMessageId = requestRow.assistant_message_id;
      await resetRequestForRetry(env, requestId, principal.user.google_sub, assistantMessageId, now);
    } else {
      userMessageId = newId();
      assistantMessageId = newId();
      const created = await createRequest(env, {
        requestId,
        conversationId: conversation.id,
        ownerSub: principal.user.google_sub,
        userMessageId,
        assistantMessageId,
        now,
      });
      if (!created) {
        const raced = await getRequest(env, requestId);
        if (!raced) {
          await releaseConversationLock(env, conversation.id, requestId, now);
          return json({ error: "could not create the request" }, 500);
        }
        if (raced.status !== "failed") {
          await releaseConversationLock(env, conversation.id, requestId, now);
          return json({ error: "this request is already in progress" }, 409);
        }
        await resetRequestForRetry(env, requestId, principal.user.google_sub, raced.assistant_message_id, now);
        userMessageId = raced.user_message_id;
        assistantMessageId = raced.assistant_message_id;
        requestRow = raced;
      }
    }

    const messages = await ensureMessages(
      env,
      principal,
      conversation,
      requestId,
      userMessageId,
      assistantMessageId,
      content,
    );
    await setTitleIfDefault(env, principal.user.google_sub, conversation.id, autoTitle(content), now);
    await touchConversation(env, principal.user.google_sub, conversation.id, now);

    const context: TurnContext = {
      env,
      principal,
      conversation,
      requestId,
      userMessage: messages.userMessage,
      assistantMessage: messages.assistantMessage,
      provider,
      firstTurn: messages.firstTurn,
    };
    return streamResponse(request, async (emit, signal) => {
      await runTurn(context, emit, signal);
    });
  } catch (error) {
    await markRequest(env, requestId, "failed", String((error as Error).message ?? error));
    await releaseConversationLock(env, conversation.id, requestId, nowMs());
    return json({ error: "the chat turn could not start" }, 500);
  }
}

function friendlyProviderError(error: unknown): { message: string; retryable: boolean } {
  if (error instanceof ProviderError) {
    if (error.status === 401 || error.status === 403) {
      return { message: "The configured AI provider rejected its API key.", retryable: false };
    }
    if (error.status === 429) {
      return { message: "The AI provider is rate limiting requests. Try again shortly.", retryable: true };
    }
    return { message: error.message.slice(0, 500), retryable: error.retryable };
  }
  return {
    message: String((error as Error).message ?? error).slice(0, 500) || "The AI request failed.",
    retryable: true,
  };
}

class TurnFailure extends Error {
  partial = "";
  source?: unknown;
}

async function runTurn(context: TurnContext, emit: Emitter, signal: AbortSignal): Promise<void> {
  const { env, principal, conversation, requestId, userMessage, assistantMessage, provider } = context;
  const ownerSub = principal.user.google_sub;
  const started = nowMs();
  let partial = "";

  emit.send("meta", {
    request_id: requestId,
    conversation_id: conversation.id,
    user_message_id: userMessage.id,
    assistant_message_id: assistantMessage.id,
    user_message: mapMessage(userMessage),
    provider: provider.provider.name,
    model: provider.model,
  });

  try {
    const maxCalls = intFrom(env.CHAT_MAX_TOOL_CALLS, 3);
    let remainingCalls = maxCalls;
    let toolsAllowed = maxCalls > 0;
    const rows = await listMessages(env, ownerSub, conversation.id);
    // Messages already covered by history (seq <= summarized_through) are NOT
    // sent to Chief directly.  Chief must use the history tool to retrieve them.
    const summarizedThrough = conversation.summarized_through;
    const unsummarizedRows = summarizedThrough > 0
      ? rows.filter((r) => r.seq > summarizedThrough)
      : rows;
    const llm = llmMessagesFrom(unsummarizedRows, assistantMessage.id, context.firstTurn);
    const usedCallIds = new Set<string>();
    let finalText = "";

    for (let iteration = 0; iteration < MAX_TOOL_ITERATIONS; iteration += 1) {
      let currentText = "";
      const calls: ToolCall[] = [];
      let stopReason = "";
      try {
        const chatTools = toolsAllowed
          ? [...ROUTINE_TOOLS, HISTORY_TOOL, MAIL_TOOL_CALL_TOOL, ...CONNECTOR_TOOLS]
          : [];
        for await (const event of provider.provider.stream(
          llm,
          chatTools,
          signal,
        )) {
          if (event.type === "text") {
            currentText += event.text;
            partial = currentText;
            if (event.text) emit.send("delta", { text: event.text });
          } else if (event.type === "tool_call") {
            let id = event.call.id || `call_${calls.length}`;
            if (usedCallIds.has(id)) id = `${id}_${iteration}`;
            usedCallIds.add(id);
            calls.push({ ...event.call, id });
          } else if (event.type === "done") {
            stopReason = event.stopReason;
          }
        }
      } catch (error) {
        const failure = new TurnFailure(
          error instanceof ProviderError ? error.message : String((error as Error).message ?? error),
        );
        failure.partial = currentText || partial;
        failure.source = error;
        throw failure;
      }

      if (!calls.length) {
        finalText = currentText;
        partial = currentText;
        break;
      }

      const assistantContent = currentText;

      // A provider-level refusal signal (content filter, safety, etc.) is an
      // authoritative signal that the model must not act.  Block any tool calls
      // regardless of what tool_calls were emitted.
      const REFUSAL_STOP_REASONS = [
        "content_filter",
        "safety",
        "toxic",
        "blocked",
        "refusal",
      ];
      if (REFUSAL_STOP_REASONS.some((r) => stopReason.toLowerCase().includes(r))) {
        finalText = assistantContent ||
          "I'm sorry, but I can't help with that request.";
        partial = finalText;
        break;
      }

      // If the assistant's visible text indicates refusal, do not execute any
      // tool calls -- the model's words ("I can't send that") are authoritative,
      // and a tool call alongside refusal text must not result in action.
      if (isRefusal(assistantContent)) {
        finalText = assistantContent;
        partial = assistantContent;
        break;
      }

      if (currentText) emit.send("reset", {});
      partial = "";

      await insertHiddenToolCall(env, {
        conversationId: conversation.id,
        ownerSub,
        requestId,
        content: assistantContent,
        toolCalls: calls,
      });
      llm.push({
        role: "assistant",
        content: assistantContent,
        toolCalls: calls,
      });

      for (const call of calls) {
        // Never execute a tool merely because plain assistant text claims to call it.
        // Only provider-emitted tool_call events reach here.
        const parsed = parseData(call.arguments || "{}");
        const query = typeof parsed.query === "string" ? parsed.query : "";
        emit.send("tool", {
          id: call.id,
          name: call.name,
          status: "start",
          query,
        });

        const assistantContent = currentText;
        let output: string;
        let errorCode = "";
        let resultCount = 0;
        let searched = false;

        if (isRoutineTool(call.name)) {
          // Routine tools are Worker-native and run on D1 directly.
          const result = await executeRoutineTool(
            env,
            ownerSub,
            principal.user.id,
            conversation.id,
            principal.user.timezone ?? "UTC",
            call,
          );
          output = result.output;
          errorCode = result.error ?? "";
        } else if (isMailToolCall(call.name)) {
          // Mail sending is backend-owned (encrypted Gmail tokens); proxy to it.
          const result = await executeMailToolCall(env, ownerSub, principal.user.id, call);
          output = result.output;
          errorCode = result.error ?? "";
        } else if (isConnectorTool(call.name)) {
          // Connector actions create a pending approval; nothing executes yet.
          const result = await executeConnectorTool(env, ownerSub, principal.user.id, call);
          output = result.output;
          errorCode = result.error ?? "";
        } else {
          // Worker-native history tool.
          if (remainingCalls <= 0) {
            output = "No further tool calls are allowed in this turn.";
            errorCode = "tool_limit";
          } else {
            remainingCalls -= 1;
            const result = await executeHistoryTool(env, ownerSub, conversation.id, call);
            output = result.output;
            errorCode = result.error ?? "";
            resultCount = result.resultCount;
            searched = result.searched;
          }
        }
        await insertHiddenToolResult(env, {
          conversationId: conversation.id,
          ownerSub,
          requestId,
          call,
          output,
          error: errorCode,
        });
        llm.push({
          role: "tool",
          content: output,
          toolCallId: call.id,
          name: call.name,
        });
        emit.send("tool", {
          id: call.id,
          name: call.name,
          status: errorCode ? "error" : "ok",
          query,
          result_count: resultCount,
          error: errorCode,
          searched,
        });
      }

      toolsAllowed = remainingCalls > 0;
      if (iteration === MAX_TOOL_ITERATIONS - 1) {
        throw new TurnFailure("The AI kept requesting tools without producing an answer.");
      }
    }

    if (!finalText.trim()) {
      throw new TurnFailure("The AI returned an empty response.");
    }

    const updated = await updateMessage(env, ownerSub, assistantMessage.id, {
      content: finalText,
      data: {
        provider: provider.provider.name,
        model: provider.model,
        elapsed_ms: nowMs() - started,
      },
    });
    await markRequest(env, requestId, "completed");
    emit.send("done", {
      request_id: requestId,
      message: updated ? mapMessage(updated) : mapMessage(assistantMessage),
    });
  } catch (error) {
    const failure =
      error instanceof TurnFailure
        ? friendlyProviderError(error.source ?? new Error(error.message))
        : friendlyProviderError(error);
    const partialText =
      error instanceof TurnFailure && error.partial.trim() ? `${error.partial.trim()}\n\n` : "";
    const message = `${partialText}[${failure.message}]`;
    const updated = await updateMessage(env, ownerSub, assistantMessage.id, {
      content: message,
      data: { error: true, retryable: failure.retryable },
    });
    await markRequest(env, requestId, "failed", failure.message);
    if (!signal.aborted) {
      emit.send("error", {
        request_id: requestId,
        error: failure.message,
        retryable: failure.retryable,
        message: updated ? mapMessage(updated) : null,
      });
    }
  } finally {
    await releaseConversationLock(env, conversation.id, requestId, nowMs());
  }
}

/* -------------------------------------------------------------------------- */
/* Dispatcher                                                                  */
/* -------------------------------------------------------------------------- */

export async function handleChatRoutes(
  request: Request,
  env: Env,
  url: URL,
): Promise<Response | null> {
  const path = url.pathname;
  if (!isChatPath(path)) return null;

  const principal = await resolvePrincipal(env, request);
  if (!principal) return json({ error: "authentication required" }, 401);
  if (!SAFE_METHODS.has(request.method)) {
    const failure = requireCsrf(request, principal);
    if (failure) return failure;
  }

  if (path === "/chat/status") {
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405);
    return json({
      provider: providerStatus(env),
      historySearch: searchConfigured(env),
    });
  }

  if (path === "/threads") {
    if (request.method === "GET") return handleList(env, principal);
    if (request.method === "POST") return handleCreate(env, principal, request);
    return json({ error: "method not allowed" }, 405);
  }

  const segments = parts(path);
  const conversationId = segments[1] ? decodeURIComponent(segments[1]) : "";
  if (!conversationId) return json({ error: "conversation not found" }, 404);

  if (segments.length === 2) {
    const conversation = await getConversation(env, principal.user.google_sub, conversationId);
    if (!conversation) return json({ error: "conversation not found" }, 404);
    if (request.method === "GET") return json(mapConversation(conversation));
    if (request.method === "PATCH") return handleRename(env, principal, conversationId, request);
    if (request.method === "DELETE") return handleDelete(env, principal, conversationId);
    return json({ error: "method not allowed" }, 405);
  }

  if (segments.length === 3 && segments[2] === "messages") {
    if (request.method === "GET") return handleMessages(env, principal, conversationId);
    if (request.method === "POST") {
      const conversation = await getConversation(env, principal.user.google_sub, conversationId);
      if (!conversation) return json({ error: "conversation not found" }, 404);
      return startTurn(request, env, principal, conversation);
    }
    return json({ error: "method not allowed" }, 405);
  }

  if (segments.length === 3 && segments[2] === "generate-history") {
    if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
    const conversation = await getConversation(env, principal.user.google_sub, conversationId);
    if (!conversation) return json({ error: "conversation not found" }, 404);
    const ownerSub = principal.user.google_sub;
    const now = nowMs();
    const tzRow = await env.DB.prepare("SELECT timezone FROM users WHERE id = ?1")
      .bind(principal.user.id)
      .first<{ timezone: string }>();
    const timezone = tzRow?.timezone ?? "UTC";
    const { date: localDate } = localDay(timezone, now);

    // Serialize generation for this conversation so overlapping requests cannot
    // process the same source messages at the same time.
    const locked = await acquireConversationLock(
      env,
      ownerSub,
      conversation.id,
      `genhist:${conversation.id}`,
      now,
    );
    if (!locked) {
      return json(
        { error: "history generation is already in progress for this channel", localDate },
        409,
      );
    }

    try {
      // Capture an explicit source-message cutoff when the job begins. Messages
      // sent after this point stay unprocessed for the next generation.
      const cutoffSeq = await lastVisibleSeq(env, conversation.id);
      const result = await generateIncrementalHistory(
        env,
        ownerSub,
        conversation.id,
        cutoffSeq,
        now,
      );
      if (!result) {
        return json(
          { error: "no new messages to summarize", localDate },
          409,
        );
      }

      // Do not report success unless the summary was actually persisted.
      if (!result.published) {
        return json(
          { error: "history coverage did not extend; nothing was persisted", localDate },
          409,
        );
      }

      return json({
        ok: true,
        localDate,
        summary: result.summary,
        fromSeq: result.fromSeq,
        toSeq: result.toSeq,
        messageCount: result.messageCount,
        published: result.published,
        sourceMessages: result.sourceMessages,
        indexed: result.indexed,
        indexSkipped: result.indexSkipped,
        indexError: result.indexError,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (/no summary model is configured/i.test(message)) {
        return json({ error: "summarisation is not configured", localDate }, 503);
      }
      return json({ error: `history generation failed: ${message}`, localDate }, 500);
    } finally {
      await releaseConversationLock(
        env,
        conversation.id,
        `genhist:${conversation.id}`,
        nowMs(),
      );
    }
  }

  return json({ error: "not found" }, 404);
}
