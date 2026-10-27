/**
 * Calendar-month compaction: daily -> monthly.
 *
 * Runs for every eligible channel regardless of Developer Mode (which only
 * suppresses the automatic daily path).  One AI request per channel per month,
 * built only from that channel's already-saved daily summaries -- never from
 * the original transcript.  The resulting monthly summary replaces only that
 * channel's daily summaries for that exact month, once it is indexed.
 */
import type { Env } from "../env";
import { localDay } from "../timezone";
import {
  claimSummaryScheduleJobsByType,
  enqueueCompactionJob,
  markSummaryScheduleJob,
  type SummaryScheduleJobRow,
} from "./schedule-store";
import {
  getChannelSummary,
  listDailySummariesForMonth,
  markChannelSummariesCompacted,
  saveChannelSummary,
  type ChannelDailySummaryRow,
} from "./store";
import { summarizeText } from "./history";
import { rebuildChannelDocument } from "./search";

const MONTHLY_COMPACTION_INSTRUCTION =
  "You are producing a monthly history summary from this channel's daily history " +
  "summaries. Combine them into ONE comprehensive, chronological plain-text account " +
  "that preserves exact technical terminology, named entities, decisions, dates, " +
  "configurations, user preferences, completed work, unresolved work, and important " +
  "factual details. Distinguish confirmed decisions from proposals. Do not invent " +
  "facts or silently resolve contradictions. Return only the paragraph, with no " +
  "headings, bullets, Markdown, or commentary.";

const MAX_ATTEMPTS = 5;
const LEASE_MS = 5 * 60 * 1000;
const CLAIMS_PER_TICK = 10;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Last millisecond of the local calendar month that contains `ms`. */
function monthEndMs(timezone: string, ms: number): number {
  const { date: localDate } = localDay(timezone, ms);
  const [year, month] = localDate.split("-").map(Number);
  // Walk forward from the 28th to find the first day of the next local month.
  let day = 28;
  let cur = localDay(timezone, Date.UTC(year, month - 1, day));
  while (cur.date === `${year}-${pad(month)}`) {
    day += 1;
    cur = localDay(timezone, Date.UTC(year, month - 1, day));
  }
  // cur is the first day of the next month (local); step back one millisecond.
  return cur.startMs - 1;
}

export interface MonthlyCompactTickResult {
  enqueued: number;
  claimed: number;
  executed: number;
  skipped: number;
  failed: number;
}

/** Enqueue a month-end compaction job for each channel whose previous calendar
 * month has daily summaries but no monthly summary yet. */
export async function scheduleMonthlyCompaction(env: Env, now: number): Promise<MonthlyCompactTickResult> {
  const result: MonthlyCompactTickResult = {
    enqueued: 0, claimed: 0, executed: 0, skipped: 0, failed: 0,
  };

  // Look back far enough to catch up after Worker interruptions.
  const sinceMs = now - 90 * 24 * 60 * 60 * 1000;
  const owners = await env.DB.prepare(
    `SELECT DISTINCT owner_sub AS ownerSub FROM conversations
      WHERE deleted_at IS NULL
        AND last_activity_at > ?1`,
  )
    .bind(sinceMs)
    .all<{ ownerSub: string }>();

  for (const owner of owners.results ?? []) {
    const timezoneRow = await env.DB.prepare(
      "SELECT timezone FROM users WHERE google_sub = ?1",
    )
      .bind(owner.ownerSub)
      .first<{ timezone: string }>();
    const timezone = timezoneRow?.timezone ?? "UTC";

    const conversations = await env.DB.prepare(
      `SELECT id, owner_sub FROM conversations
        WHERE owner_sub = ?1 AND deleted_at IS NULL`,
    )
      .bind(owner.ownerSub)
      .all<{ id: string; owner_sub: string }>();

    for (const conversation of conversations.results ?? []) {
      const monthStart = new Date(sinceMs);
      // Candidate months: every month that could have ended between sinceMs and now.
      let cursor = new Date(Date.UTC(monthStart.getUTCFullYear(), monthStart.getUTCMonth(), 1));
      const endCursor = new Date(now);
      while (cursor <= endCursor) {
        const yearMonth = `${cursor.getUTCFullYear()}-${String(cursor.getUTCMonth() + 1).padStart(2, "0")}`;
        const ended = monthEndMs(timezone, cursor.getTime()) <= now;
        if (!ended) break;

        const already = await getChannelSummary(
          env,
          owner.ownerSub,
          conversation.id,
          "monthly",
          yearMonth,
        );
        if (already) {
          cursor = new Date(Date.UTC(cursor.getUTCFullYear(), cursor.getUTCMonth() + 1, 1));
          continue;
        }

        const dailies = await listDailySummariesForMonth(
          env,
          owner.ownerSub,
          conversation.id,
          yearMonth,
        );
        if (!dailies.length) {
          cursor = new Date(Date.UTC(cursor.getUTCFullYear(), cursor.getUTCMonth() + 1, 1));
          continue;
        }

        const pending = await env.DB.prepare(
          `SELECT 1 FROM summary_schedule_jobs
            WHERE owner_sub = ?1 AND conversation_id = ?2 AND summary_type = 'monthly'
              AND period_start = ?3 AND status IN ('pending','claimed')
            LIMIT 1`,
        )
          .bind(owner.ownerSub, conversation.id, yearMonth)
          .first<{ dummy: number }>();
        if (pending) {
          cursor = new Date(Date.UTC(cursor.getUTCFullYear(), cursor.getUTCMonth() + 1, 1));
          continue;
        }

        const jobId = `monthly:${owner.ownerSub}:${conversation.id}:${yearMonth}`;
        await enqueueCompactionJob(env, {
          jobId,
          ownerSub: owner.ownerSub,
          conversationId: conversation.id,
          localDate: yearMonth,
          summaryType: "monthly",
          periodStart: yearMonth,
          periodEnd: yearMonth,
          inputSummaryIds: dailies.map((d) => d.id),
          inputSummaryVersions: dailies.map((d) => d.version),
          now,
        });
        result.enqueued += 1;

        cursor = new Date(Date.UTC(cursor.getUTCFullYear(), cursor.getUTCMonth() + 1, 1));
      }
    }
  }

  return result;
}

export async function runMonthlyCompactTick(env: Env, now: number): Promise<MonthlyCompactTickResult> {
  const result: MonthlyCompactTickResult = {
    enqueued: 0, claimed: 0, executed: 0, skipped: 0, failed: 0,
  };

  const jobs = await claimSummaryScheduleJobsByType(
    env, now, "monthly", MAX_ATTEMPTS, LEASE_MS, CLAIMS_PER_TICK,
  );
  result.claimed = jobs.length;

  for (const job of jobs) {
    try {
      const outcome = await processMonthlyJob(env, job, now);
      if (outcome === "skipped") result.skipped += 1;
      else if (outcome === "executed") result.executed += 1;
      else result.failed += 1;
    } catch (error) {
      await markSummaryScheduleJob(env, job.job_id, "failed", {
        error: error instanceof Error ? error.message : String(error),
        now,
      });
      result.failed += 1;
    }
  }

  return result;
}

async function processMonthlyJob(
  env: Env,
  job: SummaryScheduleJobRow,
  now: number,
): Promise<"executed" | "skipped" | "failed"> {
  const conversation = await env.DB.prepare(
    `SELECT id, owner_sub, title FROM conversations WHERE id = ?1`,
  )
    .bind(job.conversation_id)
    .first<{ id: string; owner_sub: string; title: string | null }>();
  if (!conversation || conversation.owner_sub !== job.owner_sub) {
    await markSummaryScheduleJob(env, job.job_id, "failed", { error: "conversation not found", now });
    return "failed";
  }

  const periodStart = job.period_start ?? job.local_date;
  if (!periodStart) {
    await markSummaryScheduleJob(env, job.job_id, "failed", { error: "missing period", now });
    return "failed";
  }

  // Idempotency: a monthly summary for this exact period already succeeded.
  const existing = await getChannelSummary(
    env,
    job.owner_sub,
    job.conversation_id,
    "monthly",
    periodStart,
  );
  if (existing && existing.generation_status === "success") {
    await markSummaryScheduleJob(env, job.job_id, "completed", { now });
    return "skipped";
  }

  const dailies = await listDailySummariesForMonth(
    env,
    job.owner_sub,
    job.conversation_id,
    periodStart,
  );
  if (!dailies.length) {
    await markSummaryScheduleJob(env, job.job_id, "completed", { now });
    return "skipped";
  }

  const transcript = dailies
    .map((d) => `${d.period_start}\n${(d.content || "").trim()}`)
    .join("\n\n---\n\n");
  const summary = await summarizeText(env, MONTHLY_COMPACTION_INSTRUCTION, transcript);

  const content = `${periodStart}\n\n${summary}`;
  const id = `monthly:${job.owner_sub}:${job.conversation_id}:${periodStart}`;
  const sourceRevision = await hashCoverage(job.conversation_id, dailies.map((d) => d.id));

  const saved = await saveChannelSummary(env, {
    id,
    ownerSub: job.owner_sub,
    conversationId: job.conversation_id,
    summaryType: "monthly",
    periodStart,
    periodEnd: job.local_date ?? periodStart,
    localDate: periodStart,
    content,
    coveredFromSeq: Math.min(...dailies.map((d) => d.covered_from_seq)),
    coveredToSeq: Math.max(...dailies.map((d) => d.covered_to_seq)),
    sourceSummaryIds: dailies.map((d) => d.id),
    sourceRevision,
    now,
  });

  if (!saved.applied && !saved.skipped) {
    await markSummaryScheduleJob(env, job.job_id, "failed", { error: "coverage did not extend", now });
    return "failed";
  }

  // Build the canonical document WITHOUT the October dailies, WITH the monthly.
  // Index it.  Only once indexing succeeds are the dailies compacted.
  const fresh = await getChannelSummary(
    env,
    job.owner_sub,
    job.conversation_id,
    "monthly",
    periodStart,
  );
  if (!fresh) {
    await markSummaryScheduleJob(env, job.job_id, "failed", { error: "summary not persisted", now });
    return "failed";
  }

  const indexOk = await rebuildChannelDocument(
    env,
    job.owner_sub,
    job.conversation_id,
    conversation.title,
    dailies.map((d) => d.id),
  );

  if (!indexOk.ok) {
    await markSummaryScheduleJob(env, job.job_id, "failed", { error: indexOk.error ?? "indexing failed", now });
    return "failed";
  }

  await markChannelSummariesCompacted(env, dailies.map((d) => d.id), now);
  await markSummaryScheduleJob(env, job.job_id, "completed", { now });
  return "executed";
}

async function hashCoverage(conversationId: string, ids: string[]): Promise<string> {
  const { sha256, toHex, utf8 } = await import("../encoding");
  const digest = toHex(await sha256(utf8(`${conversationId}:${ids.join(",")}`)));
  return digest.slice(0, 16);
}
