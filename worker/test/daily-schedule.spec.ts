/**
 * Daily-summary scheduling tests.
 *
 * Covers: job enqueuing for ended local days, Developer Mode suppression,
 * activity gating, duplicate prevention, expired-lease recovery, and job
 * claiming.  Uses the real D1 schema through cloudflare:test.
 */
import { env, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import { runDailyScheduleTick, recoverExpiredLeases } from "../src/chat/daily-schedule";
import {
  claimSummaryScheduleJob,
  enqueueSummaryScheduleJob,
  listSummaryScheduleJobs,
} from "../src/chat/schedule-store";
import {
  createConversation,
  insertMessage,
  listConversations,
  listMessages,
} from "../src/chat/store";
import { createSession, upsertUser } from "../src/store";
import { localDay } from "../src/timezone";

beforeAll(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
});

afterAll(() => {
  fetchMock.assertNoPendingInterceptors();
});

async function makeOwner(env: Env, developerMode = false) {
  const user = await upsertUser(env, {
    sub: `sub-${crypto.randomUUID()}`,
    email: "owner@example.com",
    email_verified: true,
    name: "Owner",
    picture: "",
  });
  await env.DB.prepare(
    "UPDATE users SET timezone = 'America/New_York', developer_mode = ? WHERE id = ?",
  )
    .bind(developerMode ? 1 : 0, user.id)
    .run();
  await createSession(env, user.id);
  return user;
}

async function addMessage(
  env: Env,
  conversationId: string,
  ownerSub: string,
  content: string,
  createdAt: number,
) {
  await insertMessage(env, {
    conversationId,
    ownerSub,
    role: "user",
    agent: "user",
    content,
    createdAt,
  });
}

describe("daily summary scheduling", () => {
  it("enqueues a job when a channel's local day has ended with no summary", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "test channel");

    const yesterdayMs = Date.now() - 25 * 60 * 60 * 1000;
    await addMessage(env, conv.id, user.google_sub, "hello", yesterdayMs);

    const result = await runDailyScheduleTick(env, Date.now());

    expect(result.eligible).toBeGreaterThanOrEqual(1);
    expect(result.enqueued).toBeGreaterThanOrEqual(1);

    const jobs = await listSummaryScheduleJobs(env, user.google_sub);
    const dailyJobs = jobs.filter(
      (j) => j.conversation_id === conv.id && j.summary_type === "daily",
    );
    expect(dailyJobs.length).toBeGreaterThanOrEqual(1);
    expect(["pending", "claimed"].includes(dailyJobs[0].status)).toBe(true);
  });

  it("skips scheduling when Developer Mode is on", async () => {
    const user = await makeOwner(env, true);
    const conv = await createConversation(env, user.google_sub, user.id, "dev channel");

    const yesterdayMs = Date.now() - 25 * 60 * 60 * 1000;
    await addMessage(env, conv.id, user.google_sub, "hello", yesterdayMs);

    const result = await runDailyScheduleTick(env, Date.now());

    expect(result.skippedDeveloperMode).toBeGreaterThanOrEqual(1);
    const jobs = await listSummaryScheduleJobs(env, user.google_sub);
    expect(jobs.length).toBe(0);
  });

  it("skips channels with no activity", async () => {
    const user = await makeOwner(env);
    await createConversation(env, user.google_sub, user.id, "empty channel");

    const result = await runDailyScheduleTick(env, Date.now());

    expect(result.eligible).toBe(0);
    expect(result.enqueued).toBe(0);
  });

  it("does not enqueue when the local day has not ended yet", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "active channel");

    const nowMs = Date.now();
    await addMessage(env, conv.id, user.google_sub, "hello", nowMs);

    const result = await runDailyScheduleTick(env, nowMs);

    expect(result.enqueued).toBe(0);
  });

  it("does not create duplicate jobs for the same (owner, channel, date)", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "dup channel");

    const yesterdayMs = Date.now() - 25 * 60 * 60 * 1000;
    await addMessage(env, conv.id, user.google_sub, "hello", yesterdayMs);

    const date = localDay("America/New_York", yesterdayMs).date;
    const jobId = `daily:${user.google_sub}:${conv.id}:${date}`;

    const first = await enqueueSummaryScheduleJob(env, {
      jobId,
      ownerSub: user.google_sub,
      conversationId: conv.id,
      localDate: date,
      now: Date.now(),
    });
    expect(first).toBe(true);

    const second = await enqueueSummaryScheduleJob(env, {
      jobId,
      ownerSub: user.google_sub,
      conversationId: conv.id,
      localDate: date,
      now: Date.now(),
    });
    expect(second).toBe(true);

    const jobs = await listSummaryScheduleJobs(env, user.google_sub);
    const dupes = jobs.filter((j) => j.job_id === jobId);
    expect(dupes.length).toBe(1);
  });

  it("resets a failed job to pending when re-enqueued", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "retry channel");

    const yesterdayMs = Date.now() - 25 * 60 * 60 * 1000;
    await addMessage(env, conv.id, user.google_sub, "hello", yesterdayMs);

    const date = localDay("America/New_York", yesterdayMs).date;
    const jobId = `daily:${user.google_sub}:${conv.id}:${date}`;

    await enqueueSummaryScheduleJob(env, {
      jobId,
      ownerSub: user.google_sub,
      conversationId: conv.id,
      localDate: date,
      now: Date.now(),
    });
    await env.DB.prepare(
      "UPDATE summary_schedule_jobs SET status = 'failed', error_code = 'boom' WHERE job_id = ?1",
    )
      .bind(jobId)
      .run();

    await enqueueSummaryScheduleJob(env, {
      jobId,
      ownerSub: user.google_sub,
      conversationId: conv.id,
      localDate: date,
      now: Date.now(),
      resetFailed: true,
    });

    const row = await env.DB.prepare(
      "SELECT status FROM summary_schedule_jobs WHERE job_id = ?1",
    )
      .bind(jobId)
      .first<{ status: string }>();
    expect(row?.status).toBe("pending");
  });

  it("claims a pending job and marks it claimed", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "claim channel");

    const yesterdayMs = Date.now() - 25 * 60 * 60 * 1000;
    await addMessage(env, conv.id, user.google_sub, "hello", yesterdayMs);

    const date = localDay("America/New_York", yesterdayMs).date;
    const jobId = `daily:${user.google_sub}:${conv.id}:${date}`;
    await enqueueSummaryScheduleJob(env, {
      jobId,
      ownerSub: user.google_sub,
      conversationId: conv.id,
      localDate: date,
      now: Date.now(),
    });

    const claimed = await claimSummaryScheduleJob(env, Date.now(), 5, 5 * 60 * 1000);
    expect(claimed).not.toBeNull();
    expect(claimed?.job_id).toBe(jobId);
    expect(claimed?.status).toBe("claimed");
    expect(claimed?.attempts).toBe(1);
  });

  it("recovers expired leases back to pending", async () => {
    const user = await makeOwner(env);
    const conv = await createConversation(env, user.google_sub, user.id, "recover channel");

    const date = "2026-10-24";
    const jobId = `daily:${user.google_sub}:${conv.id}:${date}`;
    const now = Date.now();

    await env.DB.prepare(
      `INSERT INTO summary_schedule_jobs
        (job_id, owner_sub, conversation_id, local_date, summary_type, status,
         attempts, available_at, lease_until, error_code, created_at, updated_at)
       VALUES (?1, ?2, ?3, ?4, 'daily', 'claimed', 1, ?5, ?6, '', ?5, ?5)`,
    )
      .bind(jobId, user.google_sub, conv.id, date, now - 10_000, now - 10_000)
      .run();

    const recovered = await recoverExpiredLeases(env, now);
    expect(recovered).toBe(1);

    const row = await env.DB.prepare(
      "SELECT status, lease_until FROM summary_schedule_jobs WHERE job_id = ?1",
    )
      .bind(jobId)
      .first<{ status: string; lease_until: number | null }>();
    expect(row?.status).toBe("pending");
    expect(row?.lease_until).toBeNull();
  });

  it("DST spring-forward day is 23 hours", () => {
    const when = new Date("2026-03-08T12:00:00Z").getTime();
    const { date, endMs } = localDay("America/New_York", when);
    const startMs = new Date("2026-03-08T00:00:00-05:00").getTime();
    expect(endMs - startMs).toBe(23 * 60 * 60 * 1000);
    expect(date).toBe("2026-03-08");
  });

  it("DST fall-back day is 25 hours", () => {
    const when = new Date("2026-11-01T12:00:00Z").getTime();
    const { date, endMs } = localDay("America/New_York", when);
    const startMs = new Date("2026-11-01T00:00:00-04:00").getTime();
    expect(endMs - startMs).toBe(25 * 60 * 60 * 1000);
    expect(date).toBe("2026-11-01");
  });
});
