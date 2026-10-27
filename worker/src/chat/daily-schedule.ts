/**
 * Daily-summary scheduling: Worker-native, no backend required.
 *
 * On every tick this identifies conversations whose local calendar day has
 * ended and that still need a daily summary, then enqueues (deduped) and
 * claims jobs for the generation service to pick up.  It never invokes the
 * model -- it only schedules and claims work.
 *
 * A channel is considered only when it had actual conversation activity on the
 * relevant day, and the owner's configured timezone determines the day
 * boundary (including 23-hour spring-forward and 25-hour fall-back days).
 * Developer Mode suppresses all automatic scheduling.
 */
import type { Env } from "../env";
import { localDay } from "../timezone";
import {
  claimSummaryScheduleJob,
  claimSummaryScheduleJobsByType,
  enqueueSummaryScheduleJob,
  isDailySummaryDue,
  markSummaryScheduleJob,
  type SummaryScheduleJobRow,
} from "./schedule-store";
import {
  getConversationById,
  listConversations,
  lastVisibleSeq,
  listMessages,
  acquireConversationLock,
  releaseConversationLock,
  type ConversationRow,
} from "./store";
import { generateDailySummary } from "./daily-generate";
import { applyDailySummary } from "./daily-publish";

const MAX_ATTEMPTS = 5;
const LEASE_MS = 5 * 60 * 1000;
const CLAIMS_PER_TICK = 5;
const ACTIVITY_WINDOW_DAYS = 30;

export interface ScheduleTickResult {
  eligible: number;
  enqueued: number;
  claimed: number;
  recovered: number;
  skippedDeveloperMode: number;
}

/** Run one scheduling tick: recover expired leases, enqueue due jobs, claim some. */
export async function runDailyScheduleTick(
  env: Env,
  now: number,
): Promise<ScheduleTickResult> {
  const result: ScheduleTickResult = {
    eligible: 0,
    enqueued: 0,
    claimed: 0,
    recovered: 0,
    skippedDeveloperMode: 0,
  };

  // Recover interrupted work: claimed jobs whose lease expired go back to pending.
  result.recovered = await recoverExpiredLeases(env, now);

  const owners = await distinctOwnersWithActivity(env, now, 20);
  for (const owner of owners) {
    if (await isDeveloperMode(env, owner.userId)) {
      result.skippedDeveloperMode += 1;
      continue;
    }
    const timezone = await getOwnerTimezone(env, owner.userId);
    const conversations = await listConversations(env, owner.userId);
    for (const conversation of conversations) {
      const dueDate = await dueLocalDate(env, conversation, timezone, now);
      if (!dueDate) continue;
      result.eligible += 1;
      const created = await enqueueSummaryScheduleJob(env, {
        jobId: `daily:${owner.userId}:${conversation.id}:${dueDate}`,
        ownerSub: owner.userId,
        conversationId: conversation.id,
        localDate: dueDate,
        now,
        resetFailed: true,
      });
      if (created) result.enqueued += 1;
    }
  }

  // Claim jobs so the generation service can pick them up.
  for (let i = 0; i < CLAIMS_PER_TICK; i += 1) {
    const job = await claimSummaryScheduleJob(env, now, MAX_ATTEMPTS, LEASE_MS);
    if (!job) break;
    result.claimed += 1;
  }

  return result;
}

/** The newest local date with activity whose day has ended and lacks a summary. */
async function dueLocalDate(
  env: Env,
  conversation: ConversationRow,
  timezone: string,
  now: number,
): Promise<string | null> {
  // Skip conversations with no visible activity.
  const rows = await listMessages(env, conversation.owner_sub, conversation.id, {
    visibleOnly: true,
  });
  if (!rows.length) return null;

  const newest = rows[rows.length - 1];
  const { date: localDate, endMs: dayEndMs } = localDay(timezone, newest.created_at);

  // The local day must have ended before a daily summary is due.
  if (now < dayEndMs) return null;

  // Already has a successful summary for this date?  Skip.
  if (await isDailySummaryDue(env, conversation.owner_sub, conversation.id, localDate)) {
    return null;
  }

  return localDate;
}

async function distinctOwnersWithActivity(
  env: Env,
  now: number,
  limit: number,
): Promise<{ userId: string }[]> {
  const { results } = await env.DB.prepare(
    `SELECT DISTINCT owner_sub AS userId FROM conversations
      WHERE deleted_at IS NULL
        AND last_activity_at > ?1
      ORDER BY last_activity_at DESC
      LIMIT ?2`,
  )
    .bind(now - ACTIVITY_WINDOW_DAYS * 24 * 60 * 60 * 1000, limit)
    .all<{ userId: string }>();
  return results ?? [];
}

async function getOwnerTimezone(env: Env, ownerSub: string): Promise<string> {
  const row = await env.DB.prepare("SELECT timezone FROM users WHERE google_sub = ?1")
    .bind(ownerSub)
    .first<{ timezone: string }>();
  return row?.timezone ?? "UTC";
}

async function isDeveloperMode(env: Env, ownerSub: string): Promise<boolean> {
  const row = await env.DB.prepare("SELECT developer_mode FROM users WHERE google_sub = ?1")
    .bind(ownerSub)
    .first<{ developer_mode: number }>();
  return (row?.developer_mode ?? 0) === 1;
}

/** Recovery pass: reset claimed jobs whose lease has expired so they can be retried. */
export async function recoverExpiredLeases(env: Env, now: number): Promise<number> {
  const result = await env.DB.prepare(
    `UPDATE summary_schedule_jobs
      SET status = 'pending', lease_until = NULL, updated_at = ?2
      WHERE status = 'claimed'
        AND lease_until IS NOT NULL
        AND lease_until <= ?2`,
  )
    .bind(now, now)
    .run();
  return (result.meta.changes ?? 0);
}

/**
 * Execute claimed daily-summary jobs.
 *
 * Claims pending daily jobs one at a time, generates only the messages newer
 * than the channel checkpoint, saves, advances the checkpoint, and indexes the
 * single canonical document.  Jobs that cannot run are marked failed so a later
 * tick can recover them.
 */
export interface DailySummaryTickResult {
  claimed: number;
  executed: number;
  skipped: number;
  failed: number;
}

export async function runDailySummaryTick(
  env: Env,
  now: number,
): Promise<DailySummaryTickResult> {
  const result: DailySummaryTickResult = { claimed: 0, executed: 0, skipped: 0, failed: 0 };
  const jobs = await claimSummaryScheduleJobsByType(
    env,
    now,
    "daily",
    MAX_ATTEMPTS,
    LEASE_MS,
    CLAIMS_PER_TICK,
  );
  result.claimed = jobs.length;

  for (const job of jobs) {
    const conversation = await getConversationById(env, job.conversation_id);
    if (!conversation || conversation.owner_sub !== job.owner_sub) {
      await markSummaryScheduleJob(env, job.job_id, "failed", { error: "conversation not found", now });
      result.failed += 1;
      continue;
    }

    // Serialize against concurrent chat turns and manual Generate History calls
    // for the same channel so two jobs never process overlapping source messages.
    const locked = await acquireConversationLock(
      env,
      job.owner_sub,
      conversation.id,
      `daily:${conversation.id}`,
      now,
    );
    if (!locked) {
      // Leave the job claimed; its lease expires and a later tick recovers it.
      continue;
    }

    try {
      const generated = await generateDailySummary(
        env,
        job.owner_sub,
        job.conversation_id,
        job.local_date,
        now,
        undefined,
        { afterSeq: conversation.summarized_through },
      );
      if (!generated) {
        await markSummaryScheduleJob(env, job.job_id, "completed", { now });
        result.skipped += 1;
        continue;
      }

      const applied = await applyDailySummary(
        env,
        job.owner_sub,
        job.conversation_id,
        job.local_date,
        generated.summary,
        generated.sourceCoverage.fromSeq,
        generated.sourceCoverage.toSeq,
        now,
      );

      if (!applied.saved && !applied.skipped) {
        await markSummaryScheduleJob(env, job.job_id, "failed", {
          error: "coverage did not extend",
          now,
        });
        result.failed += 1;
        continue;
      }

      await markSummaryScheduleJob(env, job.job_id, "completed", { now });
      result.executed += 1;
    } catch (error) {
      await markSummaryScheduleJob(env, job.job_id, "failed", {
        error: error instanceof Error ? error.message : String(error),
        now,
      });
      result.failed += 1;
    } finally {
      await releaseConversationLock(
        env,
        conversation.id,
        `daily:${conversation.id}`,
        now,
      );
    }
  }

  return result;
}

export { type SummaryScheduleJobRow };
