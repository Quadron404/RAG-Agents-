/**
 * Calendar-year compaction: monthly -> yearly.
 *
 * Runs for every eligible channel regardless of Developer Mode.  One AI request
 * per channel per year, built only from that channel's already-saved monthly
 * summaries -- never from the original transcript or daily summaries.  The
 * December monthly compaction must complete before the annual job for that year
 * is finalised; the scheduler defers the year until December's monthly summary
 * exists.
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
  listMonthlySummariesForYear,
  markChannelSummariesCompacted,
  saveChannelSummary,
} from "./store";
import { summarizeText } from "./history";
import { rebuildChannelDocument } from "./search";

const YEARLY_COMPACTION_INSTRUCTION =
  "You are producing a yearly history summary from this channel's monthly history " +
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

/** Last millisecond of the local calendar year that contains `ms`. */
function yearEndMs(timezone: string, ms: number): number {
  const { date: localDate } = localDay(timezone, ms);
  const year = Number(localDate.slice(0, 4));
  const nextYear = localDay(timezone, Date.UTC(year + 1, 0, 1));
  return nextYear.startMs - 1;
}

export interface YearlyCompactTickResult {
  enqueued: number;
  claimed: number;
  executed: number;
  skipped: number;
  failed: number;
}

/** Enqueue a year-end compaction job for each channel whose previous calendar
 * year has monthly summaries but no yearly summary yet.  Defers a year when
 * December's monthly summary is not yet ready. */
export async function scheduleYearlyCompaction(env: Env, now: number): Promise<YearlyCompactTickResult> {
  const result: YearlyCompactTickResult = {
    enqueued: 0, claimed: 0, executed: 0, skipped: 0, failed: 0,
  };

  const sinceMs = now - 90 * 24 * 60 * 60 * 1000;
  const owners = await env.DB.prepare(
    `SELECT DISTINCT owner_sub AS ownerSub FROM conversations
      WHERE deleted_at IS NULL AND last_activity_at > ?1`,
  )
    .bind(sinceMs)
    .all<{ ownerSub: string }>();

  for (const owner of owners.results ?? []) {
    const timezoneRow = await env.DB.prepare("SELECT timezone FROM users WHERE google_sub = ?1")
      .bind(owner.ownerSub)
      .first<{ timezone: string }>();
    const timezone = timezoneRow?.timezone ?? "UTC";

    const conversations = await env.DB.prepare(
      `SELECT id, owner_sub FROM conversations WHERE owner_sub = ?1 AND deleted_at IS NULL`,
    )
      .bind(owner.ownerSub)
      .all<{ id: string; owner_sub: string }>();

    for (const conversation of conversations.results ?? []) {
      // Years that may have ended between sinceMs and now.
      const startYear = new Date(sinceMs).getUTCFullYear();
      const endYear = new Date(now).getUTCFullYear();
      for (let year = startYear; year <= endYear; year += 1) {
        if (yearEndMs(timezone, Date.UTC(year, 11, 15)) > now) continue; // year not yet ended

        const yearString = String(year);
        const already = await getChannelSummary(
          env,
          owner.ownerSub,
          conversation.id,
          "yearly",
          yearString,
        );
        if (already) continue;

        const monthlies = await listMonthlySummariesForYear(
          env,
          owner.ownerSub,
          conversation.id,
          yearString,
        );
        if (!monthlies.length) continue;

        // Defer unless December's monthly summary exists (or December had no
        // daily summaries to compact).
        const decDailies = await env.DB.prepare(
          `SELECT 1 FROM channel_daily_summaries
            WHERE owner_sub = ?1 AND conversation_id = ?2 AND summary_type = 'daily'
              AND local_date LIKE ?3 || '-%'
              AND generation_status = 'success' AND compacted = 0
            LIMIT 1`,
        )
          .bind(owner.ownerSub, conversation.id, yearString)
          .first<{ dummy: number }>();
        const decemberMonthly = await getChannelSummary(
          env,
          owner.ownerSub,
          conversation.id,
          "monthly",
          `${yearString}-12`,
        );
        if (decDailies && !decemberMonthly) continue; // defer: December monthly not ready

        const pending = await env.DB.prepare(
          `SELECT 1 FROM summary_schedule_jobs
            WHERE owner_sub = ?1 AND conversation_id = ?2 AND summary_type = 'yearly'
              AND period_start = ?3 AND status IN ('pending','claimed')
            LIMIT 1`,
        )
          .bind(owner.ownerSub, conversation.id, yearString)
          .first<{ dummy: number }>();
        if (pending) continue;

        const jobId = `yearly:${owner.ownerSub}:${conversation.id}:${yearString}`;
        await enqueueCompactionJob(env, {
          jobId,
          ownerSub: owner.ownerSub,
          conversationId: conversation.id,
          localDate: yearString,
          summaryType: "yearly",
          periodStart: yearString,
          periodEnd: yearString,
          inputSummaryIds: monthlies.map((m) => m.id),
          inputSummaryVersions: monthlies.map((m) => m.version),
          now,
        });
        result.enqueued += 1;
      }
    }
  }

  return result;
}

export async function runYearlyCompactTick(env: Env, now: number): Promise<YearlyCompactTickResult> {
  const result: YearlyCompactTickResult = {
    enqueued: 0, claimed: 0, executed: 0, skipped: 0, failed: 0,
  };

  const jobs = await claimSummaryScheduleJobsByType(
    env, now, "yearly", MAX_ATTEMPTS, LEASE_MS, CLAIMS_PER_TICK,
  );
  result.claimed = jobs.length;

  for (const job of jobs) {
    try {
      const outcome = await processYearlyJob(env, job, now);
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

async function processYearlyJob(
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

  const existing = await getChannelSummary(
    env,
    job.owner_sub,
    job.conversation_id,
    "yearly",
    periodStart,
  );
  if (existing && existing.generation_status === "success") {
    await markSummaryScheduleJob(env, job.job_id, "completed", { now });
    return "skipped";
  }

  const monthlies = await listMonthlySummariesForYear(
    env,
    job.owner_sub,
    job.conversation_id,
    periodStart,
  );
  if (!monthlies.length) {
    await markSummaryScheduleJob(env, job.job_id, "completed", { now });
    return "skipped";
  }

  const transcript = monthlies
    .map((m) => `${m.period_start}\n${(m.content || "").trim()}`)
    .join("\n\n---\n\n");
  const summary = await summarizeText(env, YEARLY_COMPACTION_INSTRUCTION, transcript);

  const content = `${periodStart}\n\n${summary}`;
  const id = `yearly:${job.owner_sub}:${job.conversation_id}:${periodStart}`;
  const { sha256, toHex, utf8 } = await import("../encoding");
  const sourceRevision = toHex(await sha256(utf8(`${job.conversation_id}:${monthlies.map((m) => m.id).join(",")}`))).slice(0, 16);

  const saved = await saveChannelSummary(env, {
    id,
    ownerSub: job.owner_sub,
    conversationId: job.conversation_id,
    summaryType: "yearly",
    periodStart,
    periodEnd: periodStart,
    localDate: periodStart,
    content,
    coveredFromSeq: Math.min(...monthlies.map((m) => m.covered_from_seq)),
    coveredToSeq: Math.max(...monthlies.map((m) => m.covered_to_seq)),
    sourceSummaryIds: monthlies.map((m) => m.id),
    sourceRevision,
    now,
  });

  if (!saved.applied && !saved.skipped) {
    await markSummaryScheduleJob(env, job.job_id, "failed", { error: "coverage did not extend", now });
    return "failed";
  }

  const fresh = await getChannelSummary(
    env,
    job.owner_sub,
    job.conversation_id,
    "yearly",
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
    monthlies.map((m) => m.id),
  );

  if (!indexOk.ok) {
    await markSummaryScheduleJob(env, job.job_id, "failed", { error: indexOk.error ?? "indexing failed", now });
    return "failed";
  }

  await markChannelSummariesCompacted(env, monthlies.map((m) => m.id), now);
  await markSummaryScheduleJob(env, job.job_id, "completed", { now });
  return "executed";
}
