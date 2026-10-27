import { env, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { AiSearchChunk, AiSearchInstance, AiSearchNamespace, Env } from "../src/env";
import { FIRST_TURN_INSTRUCTION, handleChatRoutes, llmMessagesFrom } from "../src/chat/routes";
import { searchConversationHistory } from "../src/chat/search";
import { runSummaryTick, transcriptFrom } from "../src/chat/summaries";
import {
  createConversation,
  deleteConversation,
  getConversationSummary,
  insertMessage,
  listMessages,
  saveConversationSummary,
} from "../src/chat/store";
import { createSession, upsertUser } from "../src/store";

beforeAll(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
});

afterAll(() => {
  fetchMock.assertNoPendingInterceptors();
});

function withEnv(overrides: Partial<Env>): Env {
  return new Proxy(env, {
    get(target, key, receiver) {
      if (key in overrides) return overrides[key as keyof Env];
      return Reflect.get(target, key, receiver);
    },
  });
}

function fakeSearch(chunks: AiSearchChunk[] = []): {
  namespace: AiSearchNamespace;
  instanceName: string;
  uploads: Map<string, { content: string; metadata?: Record<string, string> }>;
  searches: Record<string, unknown>[];
} {
  const uploads = new Map<string, { content: string; metadata?: Record<string, string> }>();
  const searches: Record<string, unknown>[] = [];
  const instance: AiSearchInstance = {
    info: async () => ({ name: "test" }),
    update: async () => ({}),
    search: async (options) => {
      searches.push(options);
      return { chunks };
    },
    items: {
      upload: async (key, content, options) => {
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
  return {
    namespace,
    instanceName: `test-${crypto.randomUUID()}`,
    uploads,
    searches,
  };
}

describe("chat summaries", () => {
  it("keeps hidden rows out of the transcript but keeps tool results", () => {
    const messages = [
      {
        id: "s",
        conversation_id: "c",
        owner_sub: "u",
        seq: 1,
        role: "system",
        agent: "system",
        content: "hidden intro",
        data: "{}",
        hidden: 1,
        request_id: "",
        created_at: 1,
      },
      {
        id: "u",
        conversation_id: "c",
        owner_sub: "u",
        seq: 2,
        role: "user",
        agent: "user",
        content: "We chose Provider A.",
        data: "{}",
        hidden: 0,
        request_id: "",
        created_at: 2,
      },
      {
        id: "tc",
        conversation_id: "c",
        owner_sub: "u",
        seq: 3,
        role: "assistant",
        agent: "chief",
        content: "",
        data: "{}",
        hidden: 1,
        request_id: "",
        created_at: 3,
      },
      {
        id: "tr",
        conversation_id: "c",
        owner_sub: "u",
        seq: 4,
        role: "tool",
        agent: "tool",
        content: "Earlier note",
        data: JSON.stringify({ name: "tool_call_history" }),
        hidden: 1,
        request_id: "",
        created_at: 4,
      },
    ];
    const transcript = transcriptFrom(messages as never);
    expect(transcript).toContain("We chose Provider A.");
    expect(transcript).toContain("[tool_call_history result]");
    expect(transcript).not.toContain("hidden intro");
  });

  it("never moves a summary checkpoint backwards", async () => {
    const conversation = await createConversation(env, "summary-owner", "user-1");
    await insertMessage(env, {
      conversationId: conversation.id,
      ownerSub: "summary-owner",
      role: "user",
      content: "First turn",
    });
    const first = await saveConversationSummary(env, {
      conversationId: conversation.id,
      ownerSub: "summary-owner",
      content: "current summary",
      coveredFrom: 0,
      coveredTo: 2,
      docKey: "doc.txt",
      now: 100,
    });
    const stale = await saveConversationSummary(env, {
      conversationId: conversation.id,
      ownerSub: "summary-owner",
      content: "stale summary",
      coveredFrom: 0,
      coveredTo: 1,
      docKey: "doc.txt",
      now: 200,
    });
    expect(first.applied).toBe(true);
    expect(stale.applied).toBe(false);
    const summary = await getConversationSummary(env, conversation.id);
    expect(summary?.content).toBe("current summary");
    expect(summary?.covered_to).toBe(2);
  });

  it("summarizes an idle conversation and indexes its one stable document", async () => {
    const search = fakeSearch();
    const testEnv = withEnv({
      AI_SEARCH: search.namespace,
      AI_SEARCH_INSTANCE: search.instanceName,
      GROQ_API_KEY: "test-key",
    });
    fetchMock
      .get("https://api.groq.com")
      .intercept({ path: "/openai/v1/chat/completions", method: "POST" })
      .reply(
        200,
        [
          'data: {"choices":[{"delta":{"content":"The user chose Provider B."}}]}',
          "",
          "data: [DONE]",
          "",
        ].join("\n"),
        { headers: { "content-type": "text/event-stream" } },
      );

    const conversation = await createConversation(env, "summary-owner-2", "user-1", "Provider decision");
    await insertMessage(env, {
      conversationId: conversation.id,
      ownerSub: "summary-owner-2",
      role: "user",
      content: "We officially migrated to Provider B.",
    });
    await env.DB.prepare("UPDATE conversations SET last_activity_at = ?2 WHERE id = ?1")
      .bind(conversation.id, 1_000)
      .run();

    const first = await runSummaryTick(testEnv, 100_000, { idleSeconds: 60, jobs: 1 });
    expect(first.eligible).toBe(1);
    expect(first.summarized).toBe(1);
    expect(first.indexed).toBe(1);
    expect(search.uploads.size).toBe(1);
    const uploaded = [...search.uploads.values()][0];
    expect(uploaded.content).toContain("The user chose Provider B.");
    expect(uploaded.metadata?.owner_sub).toBe("summary-owner-2");

    const summary = await getConversationSummary(env, conversation.id);
    expect(summary?.content).toBe("The user chose Provider B.");
    expect(summary?.index_status).toBe("indexed");

    const second = await runSummaryTick(testEnv, 200_000, { idleSeconds: 60, jobs: 1 });
    expect(second.eligible).toBe(0);
    expect(second.summarized).toBe(0);
  });

  it("filters foreign and deleted conversations before the model sees them", async () => {
    const chunks: AiSearchChunk[] = [];
    const search = fakeSearch(chunks);
    const testEnv = withEnv({
      AI_SEARCH: search.namespace,
      AI_SEARCH_INSTANCE: search.instanceName,
    });
    const owner = await createConversation(env, "search-owner", "user-1", "Owner note");
    const foreign = await createConversation(env, "other-owner", "user-2", "Foreign note");
    const deleted = await createConversation(env, "search-owner", "user-1", "Deleted note");
    await deleteConversation(env, "search-owner", deleted.id);

    chunks.push(
      {
        id: "1",
        text: "Owner-only content",
        item: { key: "a.txt", metadata: { owner_sub: "search-owner", conversation_id: owner.id, conversation_title: "Owner note" } },
      },
      {
        id: "2",
        text: "Foreign content",
        item: { key: "b.txt", metadata: { owner_sub: "other-owner", conversation_id: foreign.id } },
      },
      {
        id: "3",
        text: "Deleted content",
        item: { key: "c.txt", metadata: { owner_sub: "search-owner", conversation_id: deleted.id } },
      },
    );

    const result = await searchConversationHistory(testEnv, "search-owner", owner.id, "provider");
    expect(result.ok).toBe(true);
    expect(result.resultCount).toBe(1);
    expect(result.text).toContain("Owner-only content");
    expect(result.text).not.toContain("Foreign content");
    expect(result.text).not.toContain("Deleted content");
    expect(search.searches[0]).toMatchObject({
      ai_search_options: {
        retrieval: { filters: { owner_sub: { $eq: "search-owner" } } },
      },
    });
  });
});

describe("Worker chat turn", () => {
  it("streams once, replays a completed request, and introduces Chief only on the first turn", async () => {
    const user = await upsertUser(env, {
      sub: "route-owner",
      email: "route@example.com",
      email_verified: true,
      name: "Route",
    });
    const session = await createSession(env, user.id);
    const testEnv = withEnv({ GROQ_API_KEY: "test-key" });
    const conversation = await createConversation(env, user.google_sub, user.id, "Route test");
    const bodies: Record<string, unknown>[] = [];
    fetchMock
      .get("https://api.groq.com")
      .intercept({ path: "/openai/v1/chat/completions", method: "POST" })
      .reply((options) => {
        bodies.push(JSON.parse(String((options as { body?: unknown }).body ?? "{}")) as Record<string, unknown>);
        const answer = bodies.length === 1 ? "First answer" : "Second answer";
        return {
          statusCode: 200,
          data: [
            `data: {"choices":[{"delta":{"content":"${answer}"}}]}`,
            "",
            "data: [DONE]",
            "",
          ].join("\n"),
          headers: { "content-type": "text/event-stream" },
        };
      })
      .persist();

    const send = (content: string, requestId: string) => {
      const request = new Request(`https://example.com/threads/${conversation.id}/messages`, {
        method: "POST",
        headers: {
          cookie: `rag_session=${session.token}`,
          "content-type": "application/json",
          "x-rag-csrf": session.csrf,
        },
        body: JSON.stringify({ content, request_id: requestId }),
      });
      return handleChatRoutes(request, testEnv, new URL(request.url));
    };

    const first = await send("First message", "req-first-0001");
    expect(first?.status).toBe(200);
    const firstText = await first!.text();
    expect(firstText).toContain("event: meta");
    expect(firstText).toContain("event: delta");
    expect(firstText).toContain("First answer");
    expect(firstText).toContain("event: done");
    expect(bodies).toHaveLength(1);
    const firstMessages = bodies[0].messages as { role: string; content: string }[];
    expect(firstMessages[0]).toEqual({ role: "system", content: FIRST_TURN_INSTRUCTION });

    const replay = await send("First message", "req-first-0001");
    const replayText = await replay!.text();
    expect(replayText).toContain('"replay":true');
    expect(replayText).toContain("First answer");
    expect(bodies).toHaveLength(1);

    const second = await send("Second message", "req-second-0001");
    const secondText = await second!.text();
    expect(secondText).toContain("Second answer");
    expect(bodies).toHaveLength(2);
    const secondMessages = bodies[1].messages as { role: string; content: string }[];
    expect(secondMessages.some((m) => m.role === "system")).toBe(false);
    expect(secondMessages.some((m) => m.content === FIRST_TURN_INSTRUCTION)).toBe(false);
    expect(secondMessages.map((m) => m.content)).toContain("First answer");
    expect(secondMessages.map((m) => m.content)).toContain("Second message");

    const visible = await listMessages(env, user.google_sub, conversation.id, { visibleOnly: true });
    expect(visible.map((m) => m.content)).toEqual([
      "First message",
      "First answer",
      "Second message",
      "Second answer",
    ]);
    const all = await listMessages(env, user.google_sub, conversation.id);
    expect(all.filter((m) => m.role === "system")).toHaveLength(1);
  });

  it("runs the history tool inside the turn and keeps its raw rows hidden", async () => {
    const user = await upsertUser(env, {
      sub: "tool-owner",
      email: "tool@example.com",
      email_verified: true,
    });
    const session = await createSession(env, user.id);
    const search = fakeSearch();
    const testEnv = withEnv({
      GROQ_API_KEY: "test-key",
      GROQ_BASE_URL: "https://tool-provider.test/v1",
      AI_SEARCH: search.namespace,
      AI_SEARCH_INSTANCE: search.instanceName,
    });
    const past = await createConversation(env, user.google_sub, user.id, "Provider migration");
    search.searches.length = 0;
    const originalSearch = search.namespace.get;
    search.namespace.get = () => {
      const instance = originalSearch("x");
      instance.search = async (options) => {
        search.searches.push(options);
        return {
          chunks: [
            {
              id: "hit",
              text: "On Oct 20 we officially migrated to Provider B.",
              item: {
                key: "past.txt",
                metadata: {
                  owner_sub: user.google_sub,
                  conversation_id: past.id,
                  conversation_title: "Provider migration",
                },
              },
            },
          ],
        };
      };
      return instance;
    };
    const conversation = await createConversation(env, user.google_sub, user.id, "Tool test");
    const bodies: Record<string, unknown>[] = [];
    fetchMock
      .get("https://tool-provider.test")
      .intercept({ path: "/v1/chat/completions", method: "POST" })
      .reply((options) => {
        bodies.push(JSON.parse(String((options as { body?: unknown }).body ?? "{}")) as Record<string, unknown>);
        const data =
          bodies.length === 1
            ? [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"tool_call_history","arguments":"{\\"query\\":\\"provider migration\\"}"}}]},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
                "",
              ]
            : [
                'data: {"choices":[{"delta":{"content":"Provider B is active."},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
                "",
              ];
        return {
          statusCode: 200,
          data: data.join("\n"),
          headers: { "content-type": "text/event-stream" },
        };
      })
      .persist();

    const request = new Request(`https://example.com/threads/${conversation.id}/messages`, {
      method: "POST",
      headers: {
        cookie: `rag_session=${session.token}`,
        "content-type": "application/json",
        "x-rag-csrf": session.csrf,
      },
      body: JSON.stringify({ content: "Which provider is active?", request_id: "req-tool-0001" }),
    });
    const response = await handleChatRoutes(request, testEnv, new URL(request.url));
    const text = await response!.text();
    expect(text).toContain('"name":"tool_call_history"');
    expect(text).toContain('"status":"ok"');
    expect(text).toContain("Provider B is active.");
    expect(bodies).toHaveLength(2);
    expect(search.searches[0]).toMatchObject({
      ai_search_options: { retrieval: { filters: { owner_sub: { $eq: user.google_sub } } } },
    });

    const all = await listMessages(env, user.google_sub, conversation.id);
    expect(all.filter((m) => m.hidden === 1 && m.role === "assistant")).toHaveLength(1);
    expect(all.filter((m) => m.hidden === 1 && m.role === "tool")).toHaveLength(1);
    const visible = await listMessages(env, user.google_sub, conversation.id, { visibleOnly: true });
    expect(visible.map((m) => m.content)).toEqual([
      "Which provider is active?",
      "Provider B is active.",
    ]);
    expect(visible.some((m) => m.content.includes("On Oct 20"))).toBe(false);
  });

  it("drops the system row from the LLM transcript after the first turn", () => {
    const base = {
      conversation_id: "c",
      owner_sub: "u",
      seq: 1,
      agent: "chief",
      hidden: 1,
      request_id: "",
      created_at: 1,
    };
    const rows = [
      { ...base, id: "s", role: "system", content: FIRST_TURN_INSTRUCTION, data: "{}" },
      { ...base, seq: 2, id: "u", role: "user", agent: "user", hidden: 0, content: "hi", data: "{}" },
    ];
    expect(llmMessagesFrom(rows as never, "", true)[0].role).toBe("system");
    expect(llmMessagesFrom(rows as never, "", false).some((m) => m.role === "system")).toBe(false);
  });
});
