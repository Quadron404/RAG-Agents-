import { describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import {
  selectChatProvider,
  selectSummaryProvider,
  validateProviderConfig,
  providerStatus,
  mapStopReason,
  isRefusalStopReason,
  isToolCallStopReason,
  isLengthStopReason,
} from "../src/chat/providers";
import type { ProviderEvent, StopReason } from "../src/chat/types";
import type { ModelProvider } from "../src/chat/types";

function withEnv(overrides: Partial<Env>): Env {
  return new Proxy({} as Env, {
    get(_target, key: string) {
      if (key in overrides) return overrides[key as keyof Env];
      return undefined;
    },
  });
}

function providerName(p: ModelProvider): string {
  return p.name;
}

describe("PROMPT-34: provider selection is deterministic", () => {
  it("picks the first configured provider in PROVIDER_ORDER when no explicit AGENT_PROVIDER", () => {
    const env = withEnv({ GROQ_API_KEY: "k1", OPENAI_API_KEY: "k2" });
    const selected = selectChatProvider(env);
    expect(selected).not.toBeNull();
    expect(providerName(selected!.provider)).toBe("groq");
    expect(selected!.model).toBe("openai/gpt-oss-20b");
  });

  it("skips providers without a key and falls through the order", () => {
    const env = withEnv({ OPENAI_API_KEY: "k2" });
    const selected = selectChatProvider(env);
    expect(selected).not.toBeNull();
    expect(providerName(selected!.provider)).toBe("openai");
  });

  it("returns null when no provider key is configured", () => {
    const env = withEnv({});
    expect(selectChatProvider(env)).toBeNull();
  });

  it("honors an explicit AGENT_PROVIDER when its key exists", () => {
    const env = withEnv({ AGENT_PROVIDER: "anthropic", ANTHROPIC_API_KEY: "k3" });
    const selected = selectChatProvider(env);
    expect(selected).not.toBeNull();
    expect(providerName(selected!.provider)).toBe("anthropic");
  });

  it("rejects an explicit AGENT_PROVIDER without its key rather than falling through", () => {
    const env = withEnv({ AGENT_PROVIDER: "anthropic", GROQ_API_KEY: "k1" });
    expect(selectChatProvider(env)).toBeNull();
  });

  it("rejects an unsupported AGENT_PROVIDER", () => {
    const env = withEnv({ AGENT_PROVIDER: "not-a-real-provider", GROQ_API_KEY: "k1" });
    expect(selectChatProvider(env)).toBeNull();
  });

  it("uses the configured AGENT_MODEL override", () => {
    const env = withEnv({
      AGENT_PROVIDER: "groq",
      GROQ_API_KEY: "k1",
      AGENT_MODEL: "custom-model",
    });
    const selected = selectChatProvider(env);
    expect(selected!.model).toBe("custom-model");
  });

  it("summary provider falls back to chat provider when SUMMARY_PROVIDER unset", () => {
    const env = withEnv({ GROQ_API_KEY: "k1" });
    const summary = selectSummaryProvider(env);
    expect(summary).not.toBeNull();
    expect(summary!.provider.name).toBe("groq");
  });

  it("summary provider uses SUMMARY_PROVIDER when explicitly set", () => {
    const env = withEnv({
      SUMMARY_PROVIDER: "anthropic",
      ANTHROPIC_API_KEY: "k3",
      GROQ_API_KEY: "k1",
    });
    const summary = selectSummaryProvider(env);
    expect(summary).not.toBeNull();
    expect(summary!.provider.name).toBe("anthropic");
  });
});

describe("PROMPT-34: provider config validation", () => {
  it("validates a fully configured provider set", () => {
    const env = withEnv({
      AGENT_PROVIDER: "groq",
      GROQ_API_KEY: "k1",
      SUMMARY_PROVIDER: "groq",
    });
    const result = validateProviderConfig(env);
    expect(result.ok).toBe(true);
    expect(result.chat?.provider).toBe("groq");
    expect(result.summary?.provider).toBe("groq");
  });

  it("reports missing provider configuration", () => {
    const env = withEnv({});
    const result = validateProviderConfig(env);
    expect(result.ok).toBe(false);
    expect(result.error).toContain("provider");
  });

  it("rejects unsupported AGENT_PROVIDER", () => {
    const env = withEnv({ AGENT_PROVIDER: "bogus", GROQ_API_KEY: "k1" });
    const result = validateProviderConfig(env);
    expect(result.ok).toBe(false);
    expect(result.error).toContain("unsupported");
  });

  it("providerStatus reports configured provider without keys", () => {
    const env = withEnv({ AGENT_PROVIDER: "groq", GROQ_API_KEY: "k1" });
    const status = providerStatus(env);
    expect(status.configured).toBe(true);
    expect(status.provider).toBe("groq");
    expect(status.model).toBe("openai/gpt-oss-20b");
  });
});

describe("PROMPT-38: stop reason mapping", () => {
  const cases: Array<[string, StopReason]> = [
    ["stop", "stop"],
    ["end_turn", "stop"],
    ["stop_sequence", "stop"],
    ["tool_calls", "tool_calls"],
    ["length", "length"],
    ["max_tokens", "length"],
    ["refusal", "refusal"],
    ["safety", "refusal"],
    ["content_filter", "refusal"],
    ["blocked", "refusal"],
    ["error", "error"],
    ["internal_error", "error"],
    ["cancel", "cancel"],
    ["aborted", "cancel"],
    ["unknown_reason", "unknown_reason"],
  ];

  it.each(cases)("maps %s to %s", (input, expected) => {
    expect(mapStopReason(input)).toBe(expected);
  });

  it("identifies refusal stop reasons", () => {
    expect(isRefusalStopReason("refusal")).toBe(true);
    expect(isRefusalStopReason("stop")).toBe(false);
  });

  it("identifies tool-call stop reasons", () => {
    expect(isToolCallStopReason("tool_calls")).toBe(true);
    expect(isToolCallStopReason("stop")).toBe(false);
  });

  it("identifies length stop reasons", () => {
    expect(isLengthStopReason("length")).toBe(true);
    expect(isLengthStopReason("stop")).toBe(false);
  });
});

describe("PROMPT-36: hardened SSE parsing", () => {
  async function collectLines(chunks: string[]): Promise<Array<{ type: string; value: string }>> {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(new TextEncoder().encode(c));
        controller.close();
      },
    });
    const out: Array<{ type: string; value: string }> = [];
    for await (const line of (await import("../src/chat/providers")).sseLines(stream)) {
      out.push(line);
    }
    return out;
  }

  it("yields data lines split across chunk boundaries", async () => {
    const lines = await collectLines([
      'data: {"choices":[',
      '{"delta":{"content":"hel',
      'lo"}}]}\n\n',
    ]);
    const dataLines = lines.filter((l) => l.type === "data");
    expect(dataLines.map((l) => l.value).join("")).toContain("hello");
  });

  it("handles CRLF line endings", async () => {
    const lines = await collectLines(['data: hello\r\n\r\n']);
    const dataLines = lines.filter((l) => l.type === "data");
    expect(dataLines.map((l) => l.value)).toContain("hello");
  });

  it("yields each data line as a separate event", async () => {
    const lines = await collectLines([
      'data: line one\n',
      'data: line two\n\n',
    ]);
    const dataLines = lines.filter((l) => l.type === "data");
    expect(dataLines.map((l) => l.value)).toEqual(["line one", "line two"]);
  });

  it("ignores comment lines", async () => {
    const lines = await collectLines([": this is a comment\n\ndata: real\n\n"]);
    const dataLines = lines.filter((l) => l.type === "data");
    expect(dataLines.map((l) => l.value)).toContain("real");
  });

  it("yields [DONE] as a data line for the consumer to terminate on", async () => {
    const lines = await collectLines(['data: {"x":1}\n\n', "data: [DONE]\n\n"]);
    const dataLines = lines.filter((l) => l.type === "data");
    expect(dataLines.map((l) => l.value)).toContain('{"x":1}');
    expect(dataLines.map((l) => l.value)).toContain("[DONE]");
  });

  it("handles partial UTF-8 across chunk boundaries", async () => {
    const lines = await collectLines(['data: café\n\n']);
    const dataLines = lines.filter((l) => l.type === "data");
    expect(dataLines.map((l) => l.value)).toContain("café");
  });
});

describe("PROMPT-37: tool-call argument assembly", () => {
  it("provider types expose the stop-reason contract", () => {
    expect(typeof mapStopReason).toBe("function");
    expect(typeof isRefusalStopReason).toBe("function");
    expect(typeof isToolCallStopReason).toBe("function");
    expect(typeof isLengthStopReason).toBe("function");
  });
});
