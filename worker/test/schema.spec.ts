import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";

function tableInfo() {
  return env.DB.prepare("PRAGMA table_info(channel_daily_summaries)").all<{
    name: string;
    type: string;
    notnull: number;
    pk: number;
  }>();
}

describe("channel daily summaries schema", () => {
  it("has required columns", async () => {
    const result = await tableInfo();
    const cols = (result.results ?? []).map((r) => r.name);
    for (const c of [
      "id",
      "owner_sub",
      "conversation_id",
      "summary_type",
      "period_start",
      "period_end",
      "local_date",
      "content",
      "version",
      "covered_from_seq",
      "covered_to_seq",
      "source_summary_ids",
      "generation_status",
      "index_status",
      "index_error",
      "retry_count",
      "source_revision",
      "created_at",
      "updated_at",
      "indexed_at",
    ]) {
      expect(cols).toContain(c);
    }
  });

  it("marks owner-scoped write columns NOT NULL", async () => {
    const result = await tableInfo();
    const notnull = Object.fromEntries((result.results ?? []).map((r) => [r.name, r.notnull]));
    for (const c of [
      "owner_sub",
      "conversation_id",
      "summary_type",
      "period_start",
      "period_end",
      "content",
      "version",
      "source_summary_ids",
      "generation_status",
      "index_status",
      "index_error",
      "retry_count",
      "source_revision",
      "created_at",
      "updated_at",
      "local_date",
    ]) {
      expect(notnull[c]).toBe(1);
    }
    // indexed_at is the only nullable timestamp column.
    expect(notnull.indexed_at).toBe(0);
  });

  it("keys summaries by id", async () => {
    const result = await tableInfo();
    const pks = (result.results ?? [])
      .filter((r) => r.pk > 0)
      .sort((a, b) => a.pk - b.pk)
      .map(({ name, type, notnull, pk }) => ({ name, type, notnull, pk }));
    expect(pks).toEqual([{ name: "id", type: "TEXT", notnull: 1, pk: 1 }]);
  });

  it("references conversations with ON DELETE CASCADE", async () => {
    const fks = await env.DB.prepare("PRAGMA foreign_key_list(channel_daily_summaries)").all<{
      from: string;
      table: string;
      on_delete: string;
    }>();
    const conv = (fks.results ?? []).find((f) => f.from === "conversation_id");
    expect(conv).toBeDefined();
    expect(conv?.table).toBe("conversations");
    expect(conv?.on_delete).toBe("CASCADE");
  });

  it("has a unique owner + channel + type + period index", async () => {
    const idxs = await env.DB.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'channel_daily_summaries'"
    ).all<{ name: string }>();
    const names = (idxs.results ?? []).map((i) => i.name);
    expect(names).toContain("idx_channel_daily_unique");
  });

  it("rejects duplicate owner + channel + type + period", async () => {
    const now = Date.now();
    await env.DB.prepare(
      "INSERT INTO conversations (id, owner_sub, owner_id, title, created_at, updated_at, last_activity_at) VALUES (?1, ?2, ?3, 't', ?4, ?4, ?4)"
    )
      .bind("conv-unique-1", "sub-unique-1", "owner-unique-1", now)
      .run();
    await env.DB.prepare(
      "INSERT INTO channel_daily_summaries (id, owner_sub, conversation_id, summary_type, period_start, period_end, content, created_at, updated_at) VALUES (?1, ?2, ?3, 'daily', ?5, ?5, 'x', ?4, ?4)"
    )
      .bind("s-unique-1", "sub-unique-1", "conv-unique-1", now, "2026-01-01")
      .run();
    let error: unknown = null;
    try {
      await env.DB.prepare(
        "INSERT INTO channel_daily_summaries (id, owner_sub, conversation_id, summary_type, period_start, period_end, content, created_at, updated_at) VALUES (?1, ?2, ?3, 'daily', ?5, ?5, 'y', ?4, ?4)"
      )
        .bind("s-unique-2", "sub-unique-1", "conv-unique-1", now, "2026-01-01")
        .run();
    } catch (e) {
      error = e;
    }
    expect(error).not.toBeNull();
  });

  it("has supporting indexes", async () => {
    const idxs = await env.DB.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'channel_daily_summaries'"
    ).all<{ name: string }>();
    const names = (idxs.results ?? []).map((i) => i.name);
    for (const n of [
      "idx_channel_daily_owner_conv",
      "idx_channel_daily_index",
      "idx_channel_daily_generation",
      "idx_channel_daily_localdate",
    ]) {
      expect(names).toContain(n);
    }
  });
});