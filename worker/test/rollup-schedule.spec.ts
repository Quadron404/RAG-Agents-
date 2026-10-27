/**
 * 30-day roll-up period selection tests.
 */
import { env } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import { runRollupScheduleTick } from "../src/chat/rollup-schedule";
import {
  createConversation,
  insertMessage,
  saveChannelDailySummary,
} from "../src/chat/store";
import { createSession, upsertUser } from "../src/store";
import { localDay } from "../src/timezone";
import {
  claimSummaryScheduleJob,
  listSummaryScheduleJobs,
} from "../src/chat/schedule-store";

beforeAll(() => {});
afterAll(() => {});

async function makeOwner(env: Env, timezone = "America/New_York") {
  const user = await upsertUser(env, {
    sub: `sub-${crypto.randomUUID()}`,
    email: "owner@example.com",
    email_verified: true,
    name: "Owner",
    picture: "",
  });
  await env.DB.prepare("UPDATE users SET timezone = ?, developer_mode = 0 WHERE id = ?")
    .bind(timezone, user.id)
    .run();
  await createSession(env, user.id);
  return user;
}

async function makeOwnerDevMode(env: Env) {
  const u = await makeOwner(env);
  await env.DB.prepare("UPDATE users SET developer_mode = 1 WHERE google_sub = ?").bind(u.google_sub).run();
  return u;
}

async function addDailySummary(
  env: Env,
  ownerSub: string,
  conversationId: string,
  localDate: string,
) {
  await saveChannelDailySummary(env, {
    id: `daily:${ownerSub}:${conversationId}:${localDate}`,
    ownerSub,
    conversationId,
    localDate,
    periodStart: localDate,
    periodEnd: localDate,
    content: `summary for ${localDate}`,
    coveredFromSeq: 1,
    coveredToSeq: 10,
    sourceSummaryIds: [],
    sourceRevision: "rev",
    generationStatus: "success",
    now: Date.now(),
  });
}

function addNDays(date: string, n: number): string {
  const [y, m, d] = date.split("-").map(Number);
  const ms = Date.UTC(y, m - 1, d + n, 12, 0, 0);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(ms));
}

describe("30-day roll-up period selection", () => {
  it("enqueues a roll-up job when a 30-day window is complete", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "complete channel");

    const base = localDay("America/New_York", Date.now() - 60 * 24 * 60 * 60 * 1000).date;
    for (let i = 0; i < 30; i += 1) {
      const d = addNDays(base, i);
      await addDailySummary(env, user.google_sub, conv.id, d);
    }

    const result = await runRollupScheduleTick(env, Date.now());
    expect(result.eligible).toBeGreaterThanOrEqual(1);
    expect(result.enqueued).toBeGreaterThanOrEqual(1);

    const jobs = await listSummaryScheduleJobs(env, user.google_sub);
    const rollupJobs = jobs.filter((j) => j.summary_type === "rollup_30d");
    expect(rollupJobs.length).toBeGreaterThanOrEqual(1);
  });

  it("does not enqueue when days are missing in the window", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "gaps channel");

    const base = localDay("America/New_York", Date.now() - 60 * 24 * 60 * 60 * 1000).date;
    for (let i = 0; i < 30; i += 1) {
      if (i === 15) continue;
      const d = addNDays(base, i);
      await addDailySummary(env, user.google_sub, conv.id, d);
    }

    const result = await runRollupScheduleTick(env, Date.now());
    expect(result.enqueued).toBe(0);
  });

  it("excludes summaries from other channels", async () => {
    const user = await makeOwner(env);
    const convA = await createConversation(env, user.google_sub, user.id, "channel A");
    const convB = await createConversation(env, user.google_sub, user.id, "channel B");

    const base = localDay("America/New_York", Date.now() - 60 * 24 * 60 * 60 * 1000).date;
    for (let i = 0; i < 30; i += 1) {
      const d = addNDays(base, i);
      await addDailySummary(env, user.google_sub, convA.id, d);
    }

    const result = await runRollupScheduleTick(env, Date.now());
    expect(result.eligible).toBeGreaterThanOrEqual(1);

    const jobs = await listSummaryScheduleJobs(env, user.google_sub);
    const rollupJobs = jobs.filter((j) => j.summary_type === "rollup_30d");
    expect(rollupJobs.some((j) => j.conversation_id === convB.id)).toBe(false);
  });

  it("does not create duplicate jobs for the same period", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "dup channel");

    const base = localDay("America/New_York", Date.now() - 60 * 24 * 60 * 60 * 1000).date;
    for (let i = 0; i < 30; i += 1) {
      const d = addNDays(base, i);
      await addDailySummary(env, user.google_sub, conv.id, d);
    }

    await runRollupScheduleTick(env, Date.now());
    await runRollupScheduleTick(env, Date.now());

    const jobs = await listSummaryScheduleJobs(env, user.google_sub);
    const rollupJobs = jobs.filter((j) => j.summary_type === "rollup_30d" && j.period_start === base);
    expect(rollupJobs.length).toBe(1);
  });

  it("skips scheduling when Developer Mode is on", async () => {
    const user = await makeOwnerDevMode(env);
    const conv = await createConversation(env, user.google_sub, user.id, "dev channel");

    const base = localDay("America/New_York", Date.now() - 60 * 24 * 60 * 60 * 1000).date;
    for (let i = 0; i < 30; i += 1) {
      const d = addNDays(base, i);
      await addDailySummary(env, user.google_sub, conv.id, d);
    }

    const result = await runRollupScheduleTick(env, Date.now());
    expect(result.skippedDeveloperMode).toBeGreaterThanOrEqual(1);
    const jobs = await listSummaryScheduleJobs(env, user.google_sub);
    expect(jobs.length).toBe(0);
  });

  it("claims a pending roll-up job", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "claim channel");

    const base = localDay("America/New_York", Date.now() - 60 * 24 * 60 * 60 * 1000).date;
    for (let i = 0; i < 30; i += 1) {
      const d = addNDays(base, i);
      await addDailySummary(env, user.google_sub, conv.id, d);
    }

    // Directly insert a pending roll-up job (bypassing runRollupScheduleTick which
    // would claim it immediately).
    const jobId = `rollup:${user.google_sub}:${conv.id}:${base}`;
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO summary_schedule_jobs
        (job_id, owner_sub, conversation_id, local_date, summary_type, status,
         attempts, available_at, lease_until, error_code, created_at, updated_at,
         period_start, period_end, input_summary_ids, input_summary_versions)
       VALUES (?1, ?2, ?3, ?4, 'rollup_30d', 'pending', 0, ?5, NULL, '', ?5, ?5,
         ?6, ?7, '[]', '[]')`,
    )
      .bind(jobId, user.google_sub, conv.id, base, now, base, addNDays(base, 29))
      .run();

    const claimed = await claimSummaryScheduleJob(env, now, 5, 5 * 60 * 1000);
    expect(claimed).not.toBeNull();
    expect(claimed?.summary_type).toBe("rollup_30d");
    expect(claimed?.status).toBe("claimed");
  });
});
