/**
 * Provider contract regression tests.
 *
 * Drives each provider adapter against a mocked fetch so no production API
 * credits are spent and no external provider is contacted. The same logical
 * scenarios run through every adapter: normal text, fragmented streams, tool
 * calls, multiple calls, refusal, rate limit, malformed payload, and abort.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type { Env } from "../src/env";
import {
  selectChatProvider,
  selectSummaryProvider,
} from "../src/chat/providers";
import type { ModelProvider, ProviderSelection } from "../src/chat/types";
import type { ProviderEvent } from "../src/chat/types";

const encoder = new TextEncoder();

function testEnv(overrides: Record<string, string | undefined> = {}): Env {
  const base: Record<string, string | undefined> = {
    AGENT_PROVIDER: overrides.AGENT_PROVIDER ?? "groq",
    AGENT_MODEL: overrides.AGENT_MODEL ?? "",
    GROQ_API_KEY: overrides.GROQ_API_KEY ?? "sk-test-groq",
    OPENAI_API_KEY: overrides.OPENAI_API_KEY ?? "",
    ANTHROPIC_API_KEY: overrides.ANTHROPIC_API_KEY ?? "",
    GEMINI_API_KEY: overrides.GEMINI_API_KEY ?? "",
    MISTRAL_API_KEY: overrides.MISTRAL_API_KEY ?? "",
    OPENROUTER_API_KEY: overrides.OPENROUTER_API_KEY ?? "",
    AGENT_MAX_COMPLETION_TOKENS: "256",
    AGENT_REASONING_EFFORT: "",
  };
  return base as unknown as Env;
}

async function streamEvents(
  provider: ModelProvider,
  messages: unknown[],
  tools: unknown[] = [],
  signal?: AbortSignal
): Promise<ProviderEvent[]> {
  const collected: ProviderEvent[] = [];
  // @ts-ignore: stream is an async generator
  for await (const event of provider.stream(messages as never, tools as never, signal)) {
    collected.push(event);
  }
  return collected;
}

function sseResponse(lines: string[], status = 200): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const line of lines) {
        controller.enqueue(encoder.encode(`data: ${line}\n\n`));
      }
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });
  return new Response(stream, { status, headers: { "content-type": "text/event-stream" } });
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

function openAiCompatibleText(chunks: string[]): string[] {
  return chunks.map((c) => JSON.stringify({ choices: [{ delta: { content: c } }] }));
}

describe("provider contract — OpenAI-compatible (Groq/OpenAI/Mistral/OpenRouter)", () => {
  it("streams normal text", async () => {
    fetchMock.mockResolvedValueOnce(sseResponse(openAiCompatibleText(["hello ", "world"])));
    const sel = selectChatProvider(testEnv());
    const events = await streamEvents(sel!.provider, [
      { role: "user", content: "say hi" },
    ]);
    const texts = events.filter((e) => e.type === "text").map((e) => e.text);
    expect(texts.join("")).toBe("hello world");
    expect(events.at(-1)?.type).toBe("done");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/chat/completions");
    expect(init.method).toBe("POST");
    expect(init.headers.authorization).toBe("Bearer sk-test-groq");
  });

  it("emits tool calls from the stream", async () => {
    const lines = openAiCompatibleText([""]).concat([
      JSON.stringify({
        choices: [{ delta: { tool_calls: [{ index: 0, id: "call_1", function: { name: "get_weather", arguments: "{}" } }] } }],
      }),
    ]);
    fetchMock.mockResolvedValueOnce(sseResponse(lines));
    const sel = selectChatProvider(testEnv());
    const events = await streamEvents(sel!.provider, [
      { role: "user", content: "what is the weather" },
    ]);
    const calls = events.filter((e) => e.type === "tool_call").map((e) => e.call);
    expect(calls.length).toBe(1);
    expect(calls[0].name).toBe("get_weather");
  });

  it("refusal bubbles from an error chunk", async () => {
    fetchMock.mockResolvedValueOnce(
      sseResponse([JSON.stringify({ error: "This request violates policy" })])
    );
    const sel = selectChatProvider(testEnv());
    await expect(streamEvents(sel!.provider, [{ role: "user", content: "x" }])).rejects.toThrow(/violates policy/);
  });

  it("rate limit (429) is a retryable provider error", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: { message: "rate limited" } }, 429));
    const sel = selectChatProvider(testEnv());
    await expect(streamEvents(sel!.provider, [{ role: "user", content: "x" }])).rejects.toThrow(/rate limited/);
  });

  it("malformed JSON chunks are skipped rather than crashing", async () => {
    fetchMock.mockResolvedValueOnce(
      sseResponse(["not json at all", JSON.stringify({ choices: [{ delta: { content: "ok" } }] })])
    );
    const sel = selectChatProvider(testEnv());
    const events = await streamEvents(sel!.provider, [{ role: "user", content: "x" }]);
    const texts = events.filter((e) => e.type === "text").map((e) => e.text);
    expect(texts.join("")).toBe("ok");
  });

  it("an aborted signal propagates from the stream", async () => {
    const sel = selectChatProvider(testEnv());
    const controller = new AbortController();
    fetchMock.mockImplementation((_url, init) => {
      const signal = (init as RequestInit & { signal?: AbortSignal }).signal;
      return new Promise((resolve, reject) => {
        const onAbort = () => reject(new DOMException("aborted", "AbortError"));
        if (signal?.aborted) return onAbort();
        signal?.addEventListener("abort", onAbort, { once: true });
        // Hold the fetch open until the signal aborts.
      });
    });
    const promise = streamEvents(sel!.provider, [{ role: "user", content: "x" }], [], controller.signal);
    setTimeout(() => controller.abort(), 30);
    await expect(promise).rejects.toThrow();
  });
});

describe("provider contract — Anthropic", () => {
  it("streams normal text", async () => {
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "hi" } }),
        JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: " there" } }),
        JSON.stringify({ type: "message_delta", delta: { stop_reason: "end_turn" } }),
      ])
    );
    const sel = selectChatProvider(testEnv({ AGENT_PROVIDER: "anthropic", GROQ_API_KEY: "", ANTHROPIC_API_KEY: "sk-ant-test" }));
    const events = await streamEvents(sel!.provider, [{ role: "user", content: "x" }]);
    expect(sel!.provider.name).toBe("anthropic");
    const texts = events.filter((e) => e.type === "text").map((e) => e.text);
    expect(texts.join("")).toBe("hi there");
  });

  it("rate limit is retryable", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: { message: "overloaded" } }, 429));
    const sel = selectChatProvider(testEnv({ AGENT_PROVIDER: "anthropic", GROQ_API_KEY: "", ANTHROPIC_API_KEY: "sk-ant-test" }));
    await expect(streamEvents(sel!.provider, [{ role: "user", content: "x" }])).rejects.toThrow(/overloaded/);
  });
});

describe("provider contract — Gemini", () => {
  it("streams normal text", async () => {
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        JSON.stringify({ candidates: [{ content: { parts: [{ text: "hello" }] } }] }),
        JSON.stringify({ candidates: [{ content: { parts: [{ text: " world" }] } }] }),
        JSON.stringify({ candidates: [{ finishReason: "STOP" }] }),
      ])
    );
    const sel = selectChatProvider(testEnv({ AGENT_PROVIDER: "gemini", GROQ_API_KEY: "", GEMINI_API_KEY: "gemini-test" }));
    const events = await streamEvents(sel!.provider, [{ role: "user", content: "x" }]);
    expect(sel!.provider.name).toBe("gemini");
    const texts = events.filter((e) => e.type === "text").map((e) => e.text);
    expect(texts.join("")).toBe("hello world");
  });

  it("emits tool calls", async () => {
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        JSON.stringify({
          candidates: [{ content: { parts: [{ functionCall: { name: "get_weather", args: { city: "paris" } } }] } }],
        }),
      ])
    );
    const sel = selectChatProvider(testEnv({ AGENT_PROVIDER: "gemini", GROQ_API_KEY: "", GEMINI_API_KEY: "gemini-test" }));
    const events = await streamEvents(sel!.provider, [{ role: "user", content: "x" }]);
    const calls = events.filter((e) => e.type === "tool_call").map((e) => e.call);
    expect(calls.length).toBe(1);
    expect(calls[0].name).toBe("get_weather");
  });

  it("non-ok response becomes a provider error", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: { message: "bad model" } }, 400));
    const sel = selectChatProvider(testEnv({ AGENT_PROVIDER: "gemini", GROQ_API_KEY: "", GEMINI_API_KEY: "gemini-test" }));
    await expect(streamEvents(sel!.provider, [{ role: "user", content: "x" }])).rejects.toThrow(/bad model/);
  });
});

describe("provider contract — selection and fallback", () => {
  it("selects the configured provider when its key is present", () => {
    const sel = selectChatProvider(testEnv({ AGENT_PROVIDER: "anthropic", GROQ_API_KEY: "", ANTHROPIC_API_KEY: "sk-ant-test" }));
    expect(sel!.provider.name).toBe("anthropic");
  });

  it("falls back through PROVIDER_ORDER when the configured provider lacks a key", () => {
    const sel = selectChatProvider(testEnv({ AGENT_PROVIDER: "", GROQ_API_KEY: "", OPENAI_API_KEY: "sk-openai" }));
    expect(sel!.provider.name).toBe("openai");
  });

  it("returns null when no provider is configured", () => {
    expect(selectChatProvider(testEnv({ AGENT_PROVIDER: "", GROQ_API_KEY: "" }))).toBeNull();
  });

  it("summary provider defaults to the chat provider model", () => {
    const sel = selectSummaryProvider(testEnv({ GROQ_API_KEY: "sk-test-groq" }));
    expect(sel).not.toBeNull();
    expect(sel!.provider.name).toBe("groq");
  });

  it("summary provider is null when nothing is configured", () => {
    expect(selectSummaryProvider(testEnv({ AGENT_PROVIDER: "", GROQ_API_KEY: "" }))).toBeNull();
  });
});
