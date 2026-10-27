/**
 * Daily-summary generation tests.
 *
 * Covers: empty-day short-circuit, message ordering, successful summary with
 * source coverage, and multi-stage summarization of long transcripts.
 */
import { env, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import { generateDailySummary } from "../src/chat/daily-generate";
import { createConversation, insertMessage } from "../src/chat/store";
import { createSession, upsertUser } from "../src/store";
import { localDay } from "../src/timezone";

beforeAll(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
  // One shared, persistent provider mock.  Tests set `currentAnswer` before
  // each request they expect to be made.
  fetchMock
    .get("https://summary.test")
    .intercept({ path: "/v1/chat/completions", method: "POST" })
    .reply(() => {
      return {
        statusCode: 200,
        data: [
          `data: {"choices":[{"delta":{"content":"${currentAnswer}"}}]}`,
          "",
          "data: [DONE]",
          "",
        ].join("\n"),
        headers: { "content-type": "text/event-stream" },
      };
    })
    .persist();
});

afterAll(() => {
  fetchMock.assertNoPendingInterceptors();
});

let currentAnswer = "unused";

async function makeOwner(env: Env, timezone = "America/New_York") {
  const user = await upsertUser(env, {
    sub: `sub-${crypto.randomUUID()}`,
    email: "owner@example.com",
    email_verified: true,
    name: "Owner",
    picture: "",
  });
  await env.DB.prepare("UPDATE users SET timezone = ? WHERE id = ?")
    .bind(timezone, user.id)
    .run();
  await createSession(env, user.id);
  return user;
}

async function addMessages(
  env: Env,
  conversationId: string,
  ownerSub: string,
  entries: { content: string; createdAt: number }[],
) {
  for (const entry of entries) {
    await insertMessage(env, {
      conversationId,
      ownerSub,
      role: "user",
      agent: "user",
      content: entry.content,
      createdAt: entry.createdAt,
    });
    await insertMessage(env, {
      conversationId,
      ownerSub,
      role: "assistant",
      agent: "chief",
      content: `Reply to: ${entry.content}`,
      createdAt: entry.createdAt + 1,
    });
  }
}

function withEnv(overrides: Partial<Env>): Env {
  return new Proxy(env, {
    get(target, key, receiver) {
      if (key in overrides) return overrides[key as keyof Env];
      return Reflect.get(target, key, receiver);
    },
  });
}

describe("daily summary generation", () => {
  it("returns null for an empty day (no model call)", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "empty channel");

    const testEnv = withEnv({
      SUMMARY_PROVIDER: "groq",
      SUMMARY_MODEL: "test-model",
      GROQ_API_KEY: "test-key",
      GROQ_BASE_URL: "https://summary.test/v1",
    });

    const yesterday = localDay("America/New_York", Date.now() - 90_000).date;
    const result = await generateDailySummary(testEnv, user.google_sub, conv.id, yesterday, Date.now());

    expect(result).toBeNull();
  });

  it("generates a summary with correct source coverage", async () => {
    currentAnswer = "This is the daily summary of the conversation.";
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "active channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await addMessages(env, conv.id, user.google_sub, [
      { content: "First message of the day", createdAt: startMs + 1000 },
      { content: "Second message of the day", createdAt: startMs + 2000 },
    ]);

    const testEnv = withEnv({
      SUMMARY_PROVIDER: "groq",
      SUMMARY_MODEL: "test-model",
      GROQ_API_KEY: "test-key",
      GROQ_BASE_URL: "https://summary.test/v1",
    });

    const date = localDay("America/New_York", startMs).date;
    const result = await generateDailySummary(testEnv, user.google_sub, conv.id, date, Date.now());

    expect(result).not.toBeNull();
    expect(result!.summary).toBe("This is the daily summary of the conversation.");
    expect(result!.sourceCoverage.messageCount).toBe(4);
    expect(result!.sourceCoverage.fromSeq).toBeGreaterThanOrEqual(1);
    expect(result!.sourceCoverage.toSeq).toBeGreaterThanOrEqual(result!.sourceCoverage.fromSeq);
    expect(result!.provider).toBe("groq");
    expect(result!.model).toBe("test-model");
    expect(result!.stages).toBe(1);
  });

  it("returns null when there are only hidden messages on the day", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "hidden-only channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await insertMessage(env, {
      conversationId: conv.id,
      ownerSub: user.google_sub,
      role: "user",
      agent: "user",
      content: "visible",
      createdAt: startMs + 1000,
      hidden: true,
    });

    const testEnv = withEnv({
      SUMMARY_PROVIDER: "groq",
      SUMMARY_MODEL: "test-model",
      GROQ_API_KEY: "test-key",
      GROQ_BASE_URL: "https://summary.test/v1",
    });

    const date = localDay("America/New_York", startMs).date;
    const result = await generateDailySummary(testEnv, user.google_sub, conv.id, date, Date.now());

    expect(result).toBeNull();
  });

  it("orders messages by seq in the transcript", async () => {
    currentAnswer = "summary of ordered messages";
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "ordering channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await addMessages(env, conv.id, user.google_sub, [
      { content: "Alpha", createdAt: startMs + 1000 },
      { content: "Beta", createdAt: startMs + 2000 },
      { content: "Gamma", createdAt: startMs + 3000 },
    ]);

    // Verify the store returns messages in seq order.
    const stored = await env.DB.prepare(
      "SELECT content, seq FROM messages WHERE conversation_id = ?1 ORDER BY seq ASC",
    )
      .bind(conv.id)
      .all<{ content: string; seq: number }>();
    const contents = stored.results!.map((r) => r.content);
    expect(contents).toContain("Alpha");
    expect(contents).toContain("Beta");
    expect(contents).toContain("Gamma");

    const testEnv = withEnv({
      SUMMARY_PROVIDER: "groq",
      SUMMARY_MODEL: "test-model",
      GROQ_API_KEY: "test-key",
      GROQ_BASE_URL: "https://summary.test/v1",
    });

    const date = localDay("America/New_York", startMs).date;
    const result = await generateDailySummary(testEnv, user.google_sub, conv.id, date, Date.now());

    expect(result).not.toBeNull();
    expect(result!.summary).toBe("summary of ordered messages");
  });

  it("uses multi-stage summarization for long transcripts", async () => {
    currentAnswer = "multi-stage summary result";
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "long channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    const longMessage = "x".repeat(60_000);
    await addMessages(env, conv.id, user.google_sub, [
      { content: longMessage, createdAt: startMs + 1000 },
      { content: longMessage, createdAt: startMs + 2000 },
      { content: longMessage, createdAt: startMs + 3000 },
      { content: longMessage, createdAt: startMs + 4000 },
      { content: longMessage, createdAt: startMs + 5000 },
    ]);

    const testEnv = withEnv({
      SUMMARY_PROVIDER: "groq",
      SUMMARY_MODEL: "test-model",
      GROQ_API_KEY: "test-key",
      GROQ_BASE_URL: "https://summary.test/v1",
    });

    const date = localDay("America/New_York", startMs).date;
    const result = await generateDailySummary(testEnv, user.google_sub, conv.id, date, Date.now());

    expect(result).not.toBeNull();
    expect(result!.summary).toBe("multi-stage summary result");
    expect(result!.stages).toBeGreaterThanOrEqual(2);
  });

  it("throws when no summary provider is configured", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "no provider channel");

    const date = "2026-10-24";
    await expect(
      generateDailySummary(env, user.google_sub, conv.id, date, Date.now()),
    ).rejects.toThrow(/no summary model is configured/);
  });
});
