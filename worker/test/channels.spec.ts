import { env } from "cloudflare:test";
import { describe, expect, it, beforeEach } from "vitest";

function tableInfo(table: string) {
  return env.DB.prepare(`PRAGMA table_info(${table})`).all<{
    name: string;
    type: string;
    notnull: number;
    pk: number;
  }>();
}

async function setupOwner() {
  const now = Date.now();
  await env.DB.prepare(
    `INSERT INTO users (id, google_sub, email, email_verified, name, picture, timezone, developer_mode, created_at, updated_at)
     VALUES (?1, 'sub-chan-1', 'chan@test.com', 1, 'Test User', '', 'UTC', 1, ?2, ?2)`
  )
    .bind("owner-chan-1", now)
    .run();
  return { ownerSub: "sub-chan-1", ownerId: "owner-chan-1" };
}

describe("channels schema", () => {
  beforeEach(async () => {
    await env.DB.prepare("DELETE FROM channels").run();
    await env.DB.prepare("DELETE FROM conversations").run();
    await env.DB.prepare("DELETE FROM users").run();
  });

  it("has required columns", async () => {
    const result = await tableInfo("channels");
    const cols = (result.results ?? []).map((r) => r.name);
    for (const c of [
      "id",
      "owner_sub",
      "name",
      "type",
      "created_at",
      "updated_at",
      "archived_at",
      "sort_order",
    ]) {
      expect(cols).toContain(c);
    }
  });

  it("keys channels by id", async () => {
    const result = await tableInfo("channels");
    const pks = (result.results ?? [])
      .filter((r) => r.pk > 0)
      .sort((a, b) => a.pk - b.pk)
      .map(({ name, type, notnull, pk }) => ({ name, type, notnull, pk }));
    // Primary key column has pk=1, type=TEXT. notnull may be 0 or 1 in SQLite.
    expect(pks).toHaveLength(1);
    expect(pks[0].name).toBe("id");
    expect(pks[0].type).toBe("TEXT");
    expect(pks[0].pk).toBe(1);
  });

  it("has unique main channel per owner", async () => {
    const idxs = await env.DB.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'channels' AND sql LIKE '%main%'"
    ).all<{ name: string }>();
    const names = (idxs.results ?? []).map((i) => i.name);
    expect(names).toContain("idx_channels_owner_main");
  });

  it("conversations have channel_id column", async () => {
    const result = await tableInfo("conversations");
    const cols = (result.results ?? []).map((r) => r.name);
    expect(cols).toContain("channel_id");
  });

  it("conversations channel_id references channels", async () => {
    const fks = await env.DB.prepare("PRAGMA foreign_key_list(conversations)").all<{
      from: string;
      table: string;
      on_delete: string;
    }>();
    const chan = (fks.results ?? []).find((f) => f.from === "channel_id");
    expect(chan).toBeDefined();
    expect(chan?.table).toBe("channels");
    expect(chan?.on_delete).toBe("SET NULL");
  });
});

describe("channel store functions", () => {
  beforeEach(async () => {
    await env.DB.prepare("DELETE FROM channels").run();
    await env.DB.prepare("DELETE FROM conversations").run();
    await env.DB.prepare("DELETE FROM users").run();
  });

  it("creates Main channel on first use and attaches legacy conversations", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const now = Date.now();

    // Insert a legacy conversation without channel_id
    await env.DB.prepare(
      `INSERT INTO conversations (id, owner_sub, owner_id, title, created_at, updated_at, last_activity_at)
       VALUES (?1, ?2, ?3, 'Legacy', ?4, ?4, ?4)`
    )
      .bind("conv-legacy-1", ownerSub, ownerId, now)
      .run();

    // Import the store functions dynamically
    const { ensureMainChannel, listChannels, getChannel } = await import("../worker/src/chat/store");

    const main = await ensureMainChannel(env, ownerSub, ownerId);
    expect(main.type).toBe("main");
    expect(main.name).toBe("Main");

    // Legacy conversation should now have channel_id
    const conv = await env.DB.prepare("SELECT channel_id FROM conversations WHERE id = ?1")
      .bind("conv-legacy-1")
      .first<{ channel_id: string }>();
    expect(conv?.channel_id).toBe(main.id);

    // List should return Main
    const channels = await listChannels(env, ownerSub);
    expect(channels.length).toBe(1);
    expect(channels[0].id).toBe(main.id);
  });

  it("creates project channels", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const { ensureMainChannel, createChannel, listChannels } = await import("../worker/src/chat/store");

    await ensureMainChannel(env, ownerSub, ownerId);
    const ch = await createChannel(env, ownerSub, ownerId, "Project Alpha");
    expect(ch.type).toBe("project");
    expect(ch.name).toBe("Project Alpha");

    const channels = await listChannels(env, ownerSub);
    expect(channels.length).toBe(2); // Main + Project Alpha
    expect(channels[0].type).toBe("main");
    expect(channels[1].type).toBe("project");
  });

  it("renames channels", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const { ensureMainChannel, createChannel, renameChannel, getChannel } = await import("../worker/src/chat/store");

    await ensureMainChannel(env, ownerSub, ownerId);
    const ch = await createChannel(env, ownerSub, ownerId, "Old Name");
    const renamed = await renameChannel(env, ownerSub, ch.id, "New Name");
    expect(renamed?.name).toBe("New Name");

    const fetched = await getChannel(env, ownerSub, ch.id);
    expect(fetched?.name).toBe("New Name");
  });

  it("archives and restores project channels", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const { ensureMainChannel, createChannel, archiveChannel, restoreChannel, listChannels, getChannel } = await import("../worker/src/chat/store");

    await ensureMainChannel(env, ownerSub, ownerId);
    const ch = await createChannel(env, ownerSub, ownerId, "To Archive");

    const archived = await archiveChannel(env, ownerSub, ch.id);
    expect(archived).toBe(true);

    // Should not appear in listChannels (which filters archived)
    const active = await listChannels(env, ownerSub);
    expect(active.find((c) => c.id === ch.id)).toBeUndefined();

    // But getChannel should still find it
    const fetched = await getChannel(env, ownerSub, ch.id);
    expect(fetched).toBeNull(); // getChannel also filters archived

    // Restore
    const restored = await restoreChannel(env, ownerSub, ch.id);
    expect(restored).toBe(true);

    const activeAfter = await listChannels(env, ownerSub);
    expect(activeAfter.find((c) => c.id === ch.id)).toBeDefined();
  });

  it("cannot archive Main channel", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const { ensureMainChannel, archiveChannel, getChannel } = await import("../worker/src/chat/store");

    const main = await ensureMainChannel(env, ownerSub, ownerId);
    const result = await archiveChannel(env, ownerSub, main.id);
    expect(result).toBe(false);

    const fetched = await getChannel(env, ownerSub, main.id);
    expect(fetched).not.toBeNull();
    expect(fetched?.archived_at).toBeNull();
  });

  it("reorders channels", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const { ensureMainChannel, createChannel, reorderChannels, listChannels } = await import("../worker/src/chat/store");

    await ensureMainChannel(env, ownerSub, ownerId);
    const ch1 = await createChannel(env, ownerSub, ownerId, "First");
    const ch2 = await createChannel(env, ownerSub, ownerId, "Second");
    const ch3 = await createChannel(env, ownerSub, ownerId, "Third");

    // Reorder: Third, First, Second (Main stays at 0)
    await reorderChannels(env, ownerSub, [ch3.id, ch1.id, ch2.id]);

    const channels = await listChannels(env, ownerSub);
    // Main should still be first (sort_order 0)
    expect(channels[0].type).toBe("main");
    // Then the reordered project channels
    expect(channels[1].id).toBe(ch3.id);
    expect(channels[2].id).toBe(ch1.id);
    expect(channels[3].id).toBe(ch2.id);
  });

  it("creates conversation in specific channel", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const { ensureMainChannel, createChannel, getOrCreateActiveConversation } = await import("../worker/src/chat/store");

    await ensureMainChannel(env, ownerSub, ownerId);
    const ch = await createChannel(env, ownerSub, ownerId, "Test Channel");

    const conv = await getOrCreateActiveConversation(env, ownerSub, ownerId, ch.id);
    expect(conv.channel_id).toBe(ch.id);
  });
});

describe("channel routes", () => {
  beforeEach(async () => {
    await env.DB.prepare("DELETE FROM channels").run();
    await env.DB.prepare("DELETE FROM conversations").run();
    await env.DB.prepare("DELETE FROM users").run();
    await env.DB.prepare("DELETE FROM sessions").run();
  });

  async function createSession(ownerSub: string, ownerId: string) {
    const { createSession: createSessionFn } = await import("../worker/src/store");
    const session = await createSessionFn(env, ownerId);
    return session;
  }

  it("lists channels with conversations", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const session = await createSession(ownerSub, ownerId);

    const { handleChatRoutes } = await import("../worker/src/chat/routes");

    // Ensure Main exists
    const req1 = new Request("http://localhost/channels", {
      method: "GET",
      headers: { cookie: `rag_session=${session.token}`, "x-rag-csrf": session.csrf },
    });
    const res1 = await handleChatRoutes(req1, env, new URL(req1.url));
    expect(res1).not.toBeNull();
    const data1 = await res1!.json();
    expect(data1.channels).toBeDefined();
    expect(data1.channels.length).toBe(1);
    expect(data1.channels[0].type).toBe("main");
  });

  it("creates a project channel", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const session = await createSession(ownerSub, ownerId);

    const { handleChatRoutes } = await import("../worker/src/chat/routes");

    // Create Main first
    await handleChatRoutes(
      new Request("http://localhost/channels", { method: "GET", headers: { cookie: `rag_session=${session.token}` } }),
      env,
      new URL("http://localhost/channels")
    );

    // Create project channel
    const req = new Request("http://localhost/channels", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        cookie: `rag_session=${session.token}`,
        "x-rag-csrf": session.csrf,
      },
      body: JSON.stringify({ name: "My Project" }),
    });
    const res = await handleChatRoutes(req, env, new URL(req.url));
    expect(res).not.toBeNull();
    const data = await res!.json();
    expect(data.name).toBe("My Project");
    expect(data.type).toBe("project");

    // List should now have 2 channels
    const listReq = new Request("http://localhost/channels", { method: "GET", headers: { cookie: `rag_session=${session.token}` } });
    const listRes = await handleChatRoutes(listReq, env, new URL(listReq.url));
    const listData = await listRes!.json();
    expect(listData.channels.length).toBe(2);
  });

  it("creates conversation in channel", async () => {
    const { ownerSub, ownerId } = await setupOwner();
    const session = await createSession(ownerSub, ownerId);

    const { handleChatRoutes } = await import("../worker/src/chat/routes");

    // Create Main and a project channel
    await handleChatRoutes(
      new Request("http://localhost/channels", { method: "GET", headers: { cookie: `rag_session=${session.token}` } }),
      env,
      new URL("http://localhost/channels")
    );

    const createChReq = new Request("http://localhost/channels", {
      method: "POST",
      headers: { "Content-Type": "application/json", cookie: `rag_session=${session.token}`, "x-rag-csrf": session.csrf },
      body: JSON.stringify({ name: "Project" }),
    });
    const createChRes = await handleChatRoutes(createChReq, env, new URL(createChReq.url));
    const chData = await createChRes!.json();

    // Create conversation in that channel
    const convReq = new Request("http://localhost/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json", cookie: `rag_session=${session.token}`, "x-rag-csrf": session.csrf },
      body: JSON.stringify({ title: "In Channel", channel_id: chData.channel_id }),
    });
    const convRes = await handleChatRoutes(convReq, env, new URL(convReq.url));
    const convData = await convRes!.json();
    expect(convData.channel_id).toBe(chData.channel_id);
  });
});