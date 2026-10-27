/**
 * Daily-summary publish (save + index) tests.
 *
 * Covers: successful D1 save, indexing failure with retry, duplicate-job
 * prevention, unchanged-summary skip, and preservation of earlier summaries.
 * Uses a fake AI Search binding to simulate indexing success/failure.
 */
import { env } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { AiSearchNamespace, Env } from "../src/env";
import {
  publishDailySummary,
  retryFailedDailySummaryIndexes,
} from "../src/chat/daily-publish";
import {
  createConversation,
  insertMessage,
  getChannelDailySummary,
} from "../src/chat/store";
import { createSession, upsertUser } from "../src/store";
import { localDay } from "../src/timezone";

beforeAll(() => {});
afterAll(() => {});

function makeOwner(env: Env, timezone = "America/New_York") {
  const user = upsertUser(env, {
    sub: `sub-${crypto.randomUUID()}`,
    email: "owner@example.com",
    email_verified: true,
    name: "Owner",
    picture: "",
  });
  return user.then((u) => {
    env.DB.prepare("UPDATE users SET timezone = ? WHERE id = ?")
      .bind(timezone, u.id)
      .run();
    return createSession(env, u.id).then(() => u);
  });
}

function makeSearch(failUpload = false): { namespace: AiSearchNamespace; instanceName: string; uploads: Map<string, { content: string; metadata?: Record<string, string> }> } {
  const uploads = new Map<string, { content: string; metadata?: Record<string, string> }>();
  const instance: any = {
    info: async () => ({ name: "test" }),
    update: async () => ({}),
    search: async () => ({ chunks: [] }),
    items: {
      upload: async (key: string, content: string, options?: { metadata?: Record<string, string> }) => {
        if (failUpload) throw new Error("indexing failed");
        uploads.set(key, {
          content: typeof content === "string" ? content : "",
          metadata: options?.metadata,
        });
        return { key };
      },
      delete: async () => ({}),
    },
  };
  const namespace: AiSearchNamespace = {
    get: () => instance,
    create: async () => instance,
  };
  return { namespace, instanceName: "test-instance", uploads };
}

function withAiSearch(env: Env, search: { namespace: AiSearchNamespace; instanceName: string }): Env {
  return new Proxy(env, {
    get(target, key, receiver) {
      if (key === "AI_SEARCH") return search.namespace;
      if (key === "AI_SEARCH_INSTANCE") return search.instanceName;
      return Reflect.get(target, key, receiver);
    },
  });
}

function withEnv(env: Env, overrides: Partial<Env>): Env {
  return new Proxy(env, {
    get(target, key, receiver) {
      if (key in overrides) return overrides[key as keyof Env];
      return Reflect.get(target, key, receiver);
    },
  });
}

describe("daily summary publish", () => {
  it("saves a summary to D1 and indexes it", async () => {
    const search = makeSearch(false);
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "publish channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await insertMessage(env, {
      conversationId: conv.id,
      ownerSub: user.google_sub,
      role: "user",
      agent: "user",
      content: "Hello world",
      createdAt: startMs + 1000,
    });

    const searchEnv = withAiSearch(env, search);
    const fetchEnv = withEnv(searchEnv, {
      AI_SEARCH_INSTANCE: search.instanceName,
    });

    const date = localDay("America/New_York", startMs).date;
    const result = await publishDailySummary(
      fetchEnv,
      user.google_sub,
      conv.id,
      date,
      date,
      date,
      "A daily summary.",
      1,
      1,
      [],
      Date.now(),
    );

    expect(result.saved).toBe(true);
    expect(result.indexed).toBe(true);
    expect(result.version).toBe(1);

    const row = await getChannelDailySummary(fetchEnv, user.google_sub, conv.id, date);
    expect(row).not.toBeNull();
    expect(row!.content).toBe("A daily summary.");
    expect(row!.index_status).toBe("indexed");
    expect(row!.version).toBe(1);
    expect(search.uploads.size).toBe(1);
  });

  it("retains summary and schedules retry when indexing fails", async () => {
    const search = makeSearch(true);
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "fail channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await insertMessage(env, {
      conversationId: conv.id,
      ownerSub: user.google_sub,
      role: "user",
      agent: "user",
      content: "Hello world",
      createdAt: startMs + 1000,
    });

    const searchEnv = withAiSearch(env, search);
    const fetchEnv = withEnv(searchEnv, {
      AI_SEARCH_INSTANCE: search.instanceName,
    });

    const date = localDay("America/New_York", startMs).date;
    const result = await publishDailySummary(
      fetchEnv,
      user.google_sub,
      conv.id,
      date,
      date,
      date,
      "A failing summary.",
      1,
      1,
      [],
      Date.now(),
    );

    expect(result.saved).toBe(true);
    expect(result.indexed).toBe(false);
    expect(result.indexError).not.toBeNull();

    const row = await getChannelDailySummary(fetchEnv, user.google_sub, conv.id, date);
    expect(row).not.toBeNull();
    expect(row!.content).toBe("A failing summary.");
    expect(row!.index_status).toBe("failed");
    expect(row!.retry_count).toBe(1);
  });

  it("retries a failed index and succeeds", async () => {
    let shouldFail = true;
    const uploads = new Map<string, { content: string; metadata?: Record<string, string> }>();
    const instance: any = {
      info: async () => ({ name: "test" }),
      update: async () => ({}),
      search: async () => ({ chunks: [] }),
      items: {
        upload: async (key: string, content: string) => {
          if (shouldFail) throw new Error("fail");
          uploads.set(key, { content: typeof content === "string" ? content : "" });
          return { key };
        },
        delete: async () => ({}),
      },
    };
    const namespace: AiSearchNamespace = {
      get: () => instance,
      create: async () => instance,
    };

    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "retry channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await insertMessage(env, {
      conversationId: conv.id,
      ownerSub: user.google_sub,
      role: "user",
      agent: "user",
      content: "Hello world",
      createdAt: startMs + 1000,
    });

    const searchEnv = withAiSearch(env, { namespace, instanceName: "test-instance" });
    const fetchEnv = withEnv(searchEnv, {
      AI_SEARCH_INSTANCE: "test-instance",
    });

    const date = localDay("America/New_York", startMs).date;
    const result = await publishDailySummary(
      fetchEnv,
      user.google_sub,
      conv.id,
      date,
      date,
      date,
      "Retry summary.",
      1,
      1,
      [],
      Date.now(),
    );

    expect(result.saved).toBe(true);
    expect(result.indexed).toBe(false);

    shouldFail = false;
    // The pending-index query only picks jobs updated >30s ago, so backdate it.
    await env.DB.prepare(
      "UPDATE channel_daily_summaries SET updated_at = ?1 WHERE conversation_id = ?2 AND local_date = ?3",
    )
      .bind(Date.now() - 60_000, conv.id, date)
      .run();
    const retried = await retryFailedDailySummaryIndexes(fetchEnv, Date.now(), 5);
    expect(retried).toBe(1);

    const row = await getChannelDailySummary(fetchEnv, user.google_sub, conv.id, date);
    expect(row!.index_status).toBe("indexed");
  });

  it("does not regenerate unchanged summaries", async () => {
    const search = makeSearch(false);
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "unchanged channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await insertMessage(env, {
      conversationId: conv.id,
      ownerSub: user.google_sub,
      role: "user",
      agent: "user",
      content: "Hello world",
      createdAt: startMs + 1000,
    });

    const searchEnv = withAiSearch(env, search);
    const fetchEnv = withEnv(searchEnv, {
      AI_SEARCH_INSTANCE: search.instanceName,
    });

    const date = localDay("America/New_York", startMs).date;

    const first = await publishDailySummary(
      fetchEnv,
      user.google_sub,
      conv.id,
      date,
      date,
      date,
      "Same content.",
      1,
      1,
      [],
      Date.now(),
    );
    expect(first.saved).toBe(true);
    expect(first.version).toBe(1);

    const second = await publishDailySummary(
      fetchEnv,
      user.google_sub,
      conv.id,
      date,
      date,
      date,
      "Same content.",
      1,
      1,
      [],
      Date.now(),
    );
    expect(second.skipped).toBe(true);
    expect(second.saved).toBe(false);
  });

  it("does not destroy an earlier successful summary when a new generation fails", async () => {
    const search = makeSearch(false);
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "preserve channel");

    const { startMs } = localDay("America/New_York", Date.now() - 90_000);
    await insertMessage(env, {
      conversationId: conv.id,
      ownerSub: user.google_sub,
      role: "user",
      agent: "user",
      content: "First day content",
      createdAt: startMs + 1000,
    });

    const searchEnv = withAiSearch(env, search);
    const fetchEnv = withEnv(searchEnv, {
      AI_SEARCH_INSTANCE: search.instanceName,
    });

    const date = localDay("America/New_York", startMs).date;
    const first = await publishDailySummary(
      fetchEnv,
      user.google_sub,
      conv.id,
      date,
      date,
      date,
      "First day summary.",
      1,
      1,
      [],
      Date.now(),
    );
    expect(first.indexed).toBe(true);

    // Switch to failing search, but same coverage → earlier summary preserved.
    const failing = makeSearch(true);
    const failingEnv = withAiSearch(env, failing);
    const failingFetchEnv = withEnv(failingEnv, {
      AI_SEARCH_INSTANCE: failing.instanceName,
    });

    const second = await publishDailySummary(
      failingFetchEnv,
      user.google_sub,
      conv.id,
      date,
      date,
      date,
      "Failed new summary.",
      1,
      1,  // same coverage as first publish
      [],
      Date.now(),
    );

    // The earlier successful summary must still be retrievable unchanged.
    const row = await getChannelDailySummary(fetchEnv, user.google_sub, conv.id, date);
    expect(row).not.toBeNull();
    expect(row!.content).toBe("First day summary.");
    expect(row!.index_status).toBe("indexed");
  });
});
