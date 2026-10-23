/**
 * Model adapters for the Worker chat path.
 *
 * The same three wire shapes the backend supports are implemented here:
 * OpenAI-compatible streaming, Anthropic messages, and Gemini
 * streamGenerateContent.  They all expose one provider-neutral stream of text,
 * tool calls, and a stop reason, so the conversation runner does not need to
 * know which vendor answered.
 */
import type { Env } from "../env";
import {
  ProviderError,
  type LlmMessage,
  type ModelProvider,
  type ProviderEvent,
  type ProviderSelection,
  type ToolCall,
  type ToolDefinition,
} from "./types";

const DEFAULT_MODELS: Record<string, string> = {
  groq: "openai/gpt-oss-20b",
  openai: "gpt-4o-mini",
  anthropic: "claude-3-5-sonnet-latest",
  gemini: "gemini-2.0-flash",
  mistral: "mistral-small-latest",
  openrouter: "openai/gpt-4o-mini",
};

const DEFAULT_BASE_URLS: Record<string, string> = {
  groq: "https://api.groq.com/openai/v1",
  openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com",
  gemini: "https://generativelanguage.googleapis.com/v1beta",
  mistral: "https://api.mistral.ai/v1",
  openrouter: "https://openrouter.ai/api/v1",
};

const PROVIDER_ORDER = ["groq", "openai", "anthropic", "gemini", "mistral", "openrouter"];

const encoder = new TextEncoder();

function intFrom(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function keyFor(env: Env, name: string): string {
  switch (name) {
    case "groq":
      return env.GROQ_API_KEY ?? "";
    case "openai":
      return env.OPENAI_API_KEY ?? "";
    case "anthropic":
      return env.ANTHROPIC_API_KEY ?? "";
    case "gemini":
      return env.GEMINI_API_KEY ?? "";
    case "mistral":
      return env.MISTRAL_API_KEY ?? "";
    case "openrouter":
      return env.OPENROUTER_API_KEY ?? "";
    default:
      return "";
  }
}

function baseFor(env: Env, name: string): string {
  const explicit = {
    groq: env.GROQ_BASE_URL,
    openai: env.OPENAI_BASE_URL,
    anthropic: env.ANTHROPIC_BASE_URL,
    gemini: env.GEMINI_BASE_URL,
    mistral: env.MISTRAL_BASE_URL,
    openrouter: env.OPENROUTER_BASE_URL,
  }[name];
  return (explicit ?? DEFAULT_BASE_URLS[name] ?? "").replace(/\/+$/, "");
}

function modelFor(env: Env, name: string, configured: string | undefined): string {
  return (configured ?? "").trim() || DEFAULT_MODELS[name] || "";
}

/** Resolve the ordinary chat provider without inventing a configured key. */
export function selectChatProvider(env: Env): ProviderSelection | null {
  const explicit = (env.AGENT_PROVIDER ?? "").trim().toLowerCase();
  const names = explicit ? [explicit] : PROVIDER_ORDER;
  for (const name of names) {
    if (!keyFor(env, name)) continue;
    return {
      provider: buildProvider(env, name, modelFor(env, name, env.AGENT_MODEL)),
      model: modelFor(env, name, env.AGENT_MODEL),
    };
  }
  return null;
}

/** Summaries may use a smaller model, but never a different unconfigured one. */
export function selectSummaryProvider(env: Env): ProviderSelection | null {
  const explicit = (env.SUMMARY_PROVIDER ?? "").trim().toLowerCase();
  if (explicit) {
    if (!keyFor(env, explicit)) return null;
    const model = modelFor(env, explicit, env.SUMMARY_MODEL ?? env.AGENT_FALLBACK_MODEL);
    return { provider: buildProvider(env, explicit, model), model };
  }
  const base = selectChatProvider(env);
  if (!base) return null;
  const model = (env.SUMMARY_MODEL ?? env.AGENT_FALLBACK_MODEL ?? "").trim() || base.model;
  return { provider: buildProvider(env, base.provider.name, model), model };
}

export function providerStatus(env: Env): {
  configured: boolean;
  provider: string;
  model: string;
  summaryConfigured: boolean;
  summaryProvider: string;
  summaryModel: string;
} {
  const chat = selectChatProvider(env);
  const summary = selectSummaryProvider(env);
  return {
    configured: !!chat,
    provider: chat?.provider.name ?? "",
    model: chat?.model ?? "",
    summaryConfigured: !!summary,
    summaryProvider: summary?.provider.name ?? "",
    summaryModel: summary?.model ?? "",
  };
}

function buildProvider(env: Env, name: string, model: string): ModelProvider {
  const apiKey = keyFor(env, name);
  const baseUrl = baseFor(env, name);
  if (name === "anthropic") {
    return anthropicProvider(name, model, apiKey, baseUrl);
  }
  if (name === "gemini") {
    return geminiProvider(name, model, apiKey, baseUrl);
  }
  return openAiCompatibleProvider(env, name, model, apiKey, baseUrl);
}

/* -------------------------------------------------------------------------- */
/* SSE / response helpers                                                      */
/* -------------------------------------------------------------------------- */

async function* sseData(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        if (line.startsWith("data:")) {
          yield line.slice(5).trimStart();
        }
        newline = buffer.indexOf("\n");
      }
    }
    buffer += decoder.decode();
    const tail = buffer.trim();
    if (tail.startsWith("data:")) yield tail.slice(5).trimStart();
  } finally {
    reader.releaseLock();
  }
}

function contentText(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  let out = "";
  for (const part of value) {
    if (typeof part === "string") out += part;
    else if (part && typeof part === "object") {
      const text = (part as Record<string, unknown>).text;
      if (typeof text === "string") out += text;
    }
  }
  return out;
}

function argumentsJson(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "{}";
  try {
    return JSON.stringify(value);
  } catch {
    return "{}";
  }
}

async function errorFromResponse(response: Response, provider: string): Promise<ProviderError> {
  let detail = "";
  try {
    detail = (await response.text()).slice(0, 2000);
  } catch {
    detail = "";
  }
  const retryAfter = Number(response.headers.get("retry-after") ?? "");
  const retryable =
    response.status === 429 ||
    response.status >= 500 ||
    (Number.isFinite(retryAfter) && retryAfter > 0);
  return new ProviderError(
    `${provider} returned ${response.status}${detail ? `: ${detail}` : ""}`,
    response.status,
    retryable,
  );
}

function timeoutSignal(parent: AbortSignal | undefined, ms: number): { signal: AbortSignal; clear: () => void } {
  const controller = new AbortController();
  const onAbort = () => controller.abort(parent?.reason);
  if (parent) {
    if (parent.aborted) controller.abort(parent.reason);
    else parent.addEventListener("abort", onAbort, { once: true });
  }
  const timer = setTimeout(() => controller.abort(new Error("model request timed out")), ms);
  return {
    signal: controller.signal,
    clear: () => {
      clearTimeout(timer);
      parent?.removeEventListener("abort", onAbort);
    },
  };
}

function mergeToolCallName(current: string, incoming: string): string {
  if (!current) return incoming;
  if (!incoming || incoming === current) return current;
  if (incoming.startsWith(current)) return incoming;
  return current + incoming;
}

/* -------------------------------------------------------------------------- */
/* OpenAI-compatible (OpenAI, Groq, Mistral, OpenRouter)                       */
/* -------------------------------------------------------------------------- */

function wireOpenAiMessages(messages: LlmMessage[]): unknown[] {
  return messages.map((message) => {
    if (message.role === "tool") {
      const wire: Record<string, unknown> = {
        role: "tool",
        tool_call_id: message.toolCallId ?? "",
        content: message.content || "ok",
      };
      if (message.name) wire.name = message.name;
      return wire;
    }
    if (message.role === "assistant") {
      const wire: Record<string, unknown> = {
        role: "assistant",
        content: message.content || null,
      };
      if (message.toolCalls?.length) {
        wire.tool_calls = message.toolCalls.map((call) => ({
          id: call.id,
          type: "function",
          function: { name: call.name, arguments: call.arguments },
        }));
      }
      return wire;
    }
    return { role: message.role, content: message.content };
  });
}

function openAiCompatibleProvider(
  env: Env,
  name: string,
  model: string,
  apiKey: string,
  baseUrl: string,
): ModelProvider {
  const maxTokens = intFrom(env.AGENT_MAX_COMPLETION_TOKENS, 2048);
  const reasoning = (env.AGENT_REASONING_EFFORT ?? "").trim();
  return {
    name,
    model,
    async *stream(messages, tools, signal) {
      const body: Record<string, unknown> = {
        model,
        messages: wireOpenAiMessages(messages),
        stream: true,
        temperature: 0,
      };
      if (tools.length) {
        body.tools = tools.map((tool) => ({
          type: "function",
          function: {
            name: tool.name,
            description: tool.description,
            parameters: tool.parameters,
          },
        }));
        body.tool_choice = "auto";
        body.parallel_tool_calls = false;
      }
      if (name === "mistral") {
        body.max_tokens = maxTokens;
      } else {
        body.max_completion_tokens = maxTokens;
        if (name !== "anthropic") body.stream_options = { include_usage: true };
      }
      if (reasoning && name !== "mistral") body.reasoning_effort = reasoning;

      const timeout = timeoutSignal(signal, 180_000);
      try {
        const response = await fetch(`${baseUrl}/chat/completions`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            authorization: `Bearer ${apiKey}`,
          },
          body: JSON.stringify(body),
          signal: timeout.signal,
        });
        if (!response.ok) throw await errorFromResponse(response, name);
        if (!response.body) throw new ProviderError(`${name} returned an empty body`);

        const contentType = response.headers.get("content-type") ?? "";
        if (contentType.includes("application/json")) {
          const payload = (await response.json()) as Record<string, unknown>;
          const choices = Array.isArray(payload.choices) ? payload.choices : [];
          const first = (choices[0] ?? {}) as Record<string, unknown>;
          const message = (first.message ?? {}) as Record<string, unknown>;
          const text = contentText(message.content);
          if (text) yield { type: "text", text };
          for (const call of completeToolCalls(message)) yield { type: "tool_call", call };
          yield { type: "done", stopReason: String(first.finish_reason ?? "stop") };
          return;
        }

        const calls = new Map<number, { id: string; name: string; arguments: string }>();
        let stopReason = "";
        for await (const data of sseData(response.body)) {
          if (!data || data === "[DONE]") break;
          let chunk: Record<string, unknown>;
          try {
            chunk = JSON.parse(data) as Record<string, unknown>;
          } catch {
            continue;
          }
          const error = chunk.error;
          if (error) {
            throw new ProviderError(
              `${name} refused the request: ${typeof error === "string" ? error : JSON.stringify(error)}`,
              200,
              false,
            );
          }
          const choices = Array.isArray(chunk.choices) ? chunk.choices : [];
          if (!choices.length) continue;
          const first = (choices[0] ?? {}) as Record<string, unknown>;
          if (first.finish_reason) stopReason = String(first.finish_reason);
          const delta = (first.delta ?? {}) as Record<string, unknown>;
          const text = contentText(delta.content);
          if (text) yield { type: "text", text };

          const rawCalls = Array.isArray(delta.tool_calls) ? delta.tool_calls : [];
          const legacy = delta.function_call;
          const entries = rawCalls.length
            ? rawCalls
            : legacy && typeof legacy === "object"
              ? [legacy]
              : [];
          entries.forEach((entry, position) => {
            if (!entry || typeof entry !== "object") return;
            const call = entry as Record<string, unknown>;
            const index = typeof call.index === "number" ? call.index : position;
            if (!calls.has(index)) calls.set(index, { id: "", name: "", arguments: "" });
            const current = calls.get(index)!;
            const fn = (call.function ?? {}) as Record<string, unknown>;
            if (typeof call.id === "string" && call.id) current.id = call.id;
            const incomingName = String(fn.name ?? call.name ?? "");
            current.name = mergeToolCallName(current.name, incomingName);
            const args = fn.arguments ?? call.arguments;
            if (args !== null && args !== undefined && args !== "") {
              current.arguments += argumentsJson(args);
            }
          });
        }
        for (const [index, call] of calls) {
          if (!call.name) continue;
          yield {
            type: "tool_call",
            call: {
              id: call.id || `call_${index}`,
              name: call.name,
              arguments: call.arguments || "{}",
            },
          };
        }
        yield { type: "done", stopReason: stopReason || "stop" };
      } catch (error) {
        if (error instanceof ProviderError) throw error;
        if (signal?.aborted) throw error;
        throw new ProviderError(`${name} request failed: ${(error as Error).message}`, 0, true);
      } finally {
        timeout.clear();
      }
    },
  };
}

function completeToolCalls(message: Record<string, unknown>): ToolCall[] {
  const entries = Array.isArray(message.tool_calls)
    ? message.tool_calls
    : message.function_call && typeof message.function_call === "object"
      ? [message.function_call]
      : [];
  const calls: ToolCall[] = [];
  entries.forEach((entry, index) => {
    if (!entry || typeof entry !== "object") return;
    const call = entry as Record<string, unknown>;
    const fn = (call.function ?? {}) as Record<string, unknown>;
    const name = String(fn.name ?? call.name ?? "");
    if (!name) return;
    calls.push({
      id: String(call.id ?? `call_${index}`),
      name,
      arguments: argumentsJson(fn.arguments ?? call.arguments),
    });
  });
  return calls;
}

/* -------------------------------------------------------------------------- */
/* Anthropic                                                                   */
/* -------------------------------------------------------------------------- */

function anthropicMessages(messages: LlmMessage[]): { system: string; messages: unknown[] } {
  const system: string[] = [];
  const wire: unknown[] = [];
  let toolResults: { tool_use_id: string; content: string }[] = [];
  const flushTools = () => {
    if (!toolResults.length) return;
    wire.push({ role: "user", content: toolResults });
    toolResults = [];
  };
  for (const message of messages) {
    if (message.role === "system") {
      if (message.content.trim()) system.push(message.content.trim());
      continue;
    }
    if (message.role === "tool") {
      toolResults.push({
        tool_use_id: message.toolCallId ?? "",
        content: message.content || "ok",
      });
      continue;
    }
    flushTools();
    if (message.role === "assistant") {
      const content: unknown[] = [];
      if (message.content) content.push({ type: "text", text: message.content });
      for (const call of message.toolCalls ?? []) {
        let input: unknown = {};
        try {
          input = JSON.parse(call.arguments || "{}");
        } catch {
          input = {};
        }
        content.push({ type: "tool_use", id: call.id, name: call.name, input });
      }
      wire.push({ role: "assistant", content: content.length ? content : [{ type: "text", text: "" }] });
      continue;
    }
    wire.push({ role: "user", content: [{ type: "text", text: message.content }] });
  }
  flushTools();
  return { system: system.join("\n\n"), messages: wire };
}

function anthropicProvider(name: string, model: string, apiKey: string, baseUrl: string): ModelProvider {
  return {
    name,
    model,
    async *stream(messages, tools, signal) {
      const converted = anthropicMessages(messages);
      const body: Record<string, unknown> = {
        model,
        max_tokens: 2048,
        temperature: 0,
        system: converted.system,
        messages: converted.messages,
        stream: true,
      };
      if (tools.length) {
        body.tools = tools.map((tool) => ({
          name: tool.name,
          description: tool.description,
          input_schema: tool.parameters,
        }));
      }
      const timeout = timeoutSignal(signal, 180_000);
      try {
        const response = await fetch(`${baseUrl}/v1/messages`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "x-api-key": apiKey,
            "anthropic-version": "2023-06-01",
          },
          body: JSON.stringify(body),
          signal: timeout.signal,
        });
        if (!response.ok) throw await errorFromResponse(response, name);
        if (!response.body) throw new ProviderError(`${name} returned an empty body`);
        const calls = new Map<number, { id: string; name: string; arguments: string }>();
        let stopReason = "";
        for await (const data of sseData(response.body)) {
          if (!data) continue;
          let event: Record<string, unknown>;
          try {
            event = JSON.parse(data) as Record<string, unknown>;
          } catch {
            continue;
          }
          const type = String(event.type ?? "");
          if (type === "content_block_start") {
            const index = Number(event.index ?? 0);
            const block = (event.content_block ?? {}) as Record<string, unknown>;
            if (block.type === "tool_use") {
              calls.set(index, {
                id: String(block.id ?? `call_${index}`),
                name: String(block.name ?? ""),
                arguments: "",
              });
            }
          } else if (type === "content_block_delta") {
            const index = Number(event.index ?? 0);
            const delta = (event.delta ?? {}) as Record<string, unknown>;
            if (delta.type === "text_delta" && typeof delta.text === "string") {
              yield { type: "text", text: delta.text };
            } else if (delta.type === "input_json_delta" && typeof delta.partial_json === "string") {
              const current = calls.get(index);
              if (current) current.arguments += delta.partial_json;
            }
          } else if (type === "message_delta") {
            const delta = (event.delta ?? {}) as Record<string, unknown>;
            if (delta.stop_reason) stopReason = String(delta.stop_reason);
          } else if (type === "error") {
            const error = (event.error ?? {}) as Record<string, unknown>;
            throw new ProviderError(`${name} stream error: ${String(error.message ?? "unknown")}`);
          }
        }
        for (const [index, call] of calls) {
          if (!call.name) continue;
          yield {
            type: "tool_call",
            call: {
              id: call.id || `call_${index}`,
              name: call.name,
              arguments: call.arguments || "{}",
            },
          };
        }
        yield { type: "done", stopReason: stopReason || "end_turn" };
      } catch (error) {
        if (error instanceof ProviderError) throw error;
        if (signal?.aborted) throw error;
        throw new ProviderError(`${name} request failed: ${(error as Error).message}`, 0, true);
      } finally {
        timeout.clear();
      }
    },
  };
}

/* -------------------------------------------------------------------------- */
/* Gemini                                                                      */
/* -------------------------------------------------------------------------- */

function geminiContents(messages: LlmMessage[]): {
  system: string;
  contents: unknown[];
} {
  const system: string[] = [];
  const contents: unknown[] = [];
  for (const message of messages) {
    if (message.role === "system") {
      if (message.content.trim()) system.push(message.content.trim());
      continue;
    }
    if (message.role === "tool") {
      contents.push({
        role: "user",
        parts: [
          {
            functionResponse: {
              name: message.name || "tool",
              response: { result: message.content || "ok" },
            },
          },
        ],
      });
      continue;
    }
    if (message.role === "assistant") {
      const parts: unknown[] = [];
      if (message.content) parts.push({ text: message.content });
      for (const call of message.toolCalls ?? []) {
        let args: unknown = {};
        try {
          args = JSON.parse(call.arguments || "{}");
        } catch {
          args = {};
        }
        parts.push({ functionCall: { name: call.name, args } });
      }
      contents.push({ role: "model", parts: parts.length ? parts : [{ text: "" }] });
      continue;
    }
    contents.push({ role: "user", parts: [{ text: message.content }] });
  }
  return { system: system.join("\n\n"), contents };
}

function geminiProvider(name: string, model: string, apiKey: string, baseUrl: string): ModelProvider {
  return {
    name,
    model,
    async *stream(messages, tools, signal) {
      const converted = geminiContents(messages);
      const cleanModel = model.replace(/^models\//, "");
      const body: Record<string, unknown> = {
        contents: converted.contents,
        generationConfig: { temperature: 0, maxOutputTokens: 2048 },
      };
      if (converted.system) {
        body.systemInstruction = { parts: [{ text: converted.system }] };
      }
      if (tools.length) {
        body.tools = [
          {
            functionDeclarations: tools.map((tool) => ({
              name: tool.name,
              description: tool.description,
              parameters: tool.parameters,
            })),
          },
        ];
      }
      const timeout = timeoutSignal(signal, 180_000);
      try {
        const response = await fetch(
          `${baseUrl}/models/${encodeURIComponent(cleanModel)}:streamGenerateContent?alt=sse`,
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "x-goog-api-key": apiKey,
            },
            body: JSON.stringify(body),
            signal: timeout.signal,
          },
        );
        if (!response.ok) throw await errorFromResponse(response, name);
        if (!response.body) throw new ProviderError(`${name} returned an empty body`);
        const calls: ToolCall[] = [];
        let stopReason = "";
        for await (const data of sseData(response.body)) {
          if (!data) continue;
          let chunk: Record<string, unknown>;
          try {
            chunk = JSON.parse(data) as Record<string, unknown>;
          } catch {
            continue;
          }
          const candidates = Array.isArray(chunk.candidates) ? chunk.candidates : [];
          if (!candidates.length) continue;
          const candidate = (candidates[0] ?? {}) as Record<string, unknown>;
          if (candidate.finishReason) stopReason = String(candidate.finishReason);
          const content = (candidate.content ?? {}) as Record<string, unknown>;
          const parts = Array.isArray(content.parts) ? content.parts : [];
          for (const rawPart of parts) {
            if (!rawPart || typeof rawPart !== "object") continue;
            const part = rawPart as Record<string, unknown>;
            if (typeof part.text === "string" && part.text) {
              yield { type: "text", text: part.text };
            }
            const fn = part.functionCall;
            if (fn && typeof fn === "object") {
              const call = fn as Record<string, unknown>;
              const callName = String(call.name ?? "");
              if (!callName) continue;
              calls.push({
                id: `call_${calls.length}`,
                name: callName,
                arguments: argumentsJson(call.args),
              });
            }
          }
        }
        for (const call of calls) yield { type: "tool_call", call };
        yield { type: "done", stopReason: stopReason || "STOP" };
      } catch (error) {
        if (error instanceof ProviderError) throw error;
        if (signal?.aborted) throw error;
        throw new ProviderError(`${name} request failed: ${(error as Error).message}`, 0, true);
      } finally {
        timeout.clear();
      }
    },
  };
}

/** Used only when a caller already has the provider handle (tests, summaries). */
export function textFromProviderEvents(events: ProviderEvent[]): string {
  return events.map((event) => (event.type === "text" ? event.text : "")).join("");
}

export { encoder };
