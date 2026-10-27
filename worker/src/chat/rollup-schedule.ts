/**
 * 30-day roll-up scheduling: Worker-native, no backend required.
 *
 * Identifies channels whose 30-day period is complete (every day has a
 * successful daily summary) and enqueues deduped roll-up jobs.  It never
 * generates the roll-up -- it only selects periods and schedules work.
 *
 * Input is always daily summaries, never the original transcript.  Period
 * boundaries and input summary IDs/versions are tracked so changed or
 * obsolete inputs can be detected and obsolete roll-ups replaced.
 *
 * Developer Mode suppresses all automatic scheduling.
 */
import type { Env } from "../env";
import { localDay, previousLocalDate } from "../timezone";
import {
  claimSummaryScheduleJob,
  enqueueRollupJob,
  rollupAlreadyCompleted,
  rollupPeriodNeedsJob,
  type SummaryScheduleJobRow,
} from "./schedule-store";
import {
  getChannelDailySummary,
  listConversations,
  type ChannelDailySummaryRow,
} from "./store";

const MAX_ATTEMPTS = 5;
const LEASE_MS = 5 * 60 * 1000;
const CLAIMS_PER_TICK = 5;
const ACTIVITY_WINDOW_DAYS = 30;
const ROLLUP_DAYS = 30;

interface RollupTickResult {
  eligible: number;
  enqueued: number;
  claimed: number;
  skippedDeveloperMode: number;
}

/** Run one roll-up scheduling tick: enqueue due roll-up jobs, then claim some. */
export async function runRollupScheduleTick(
  env: Env,
  now: number,
): Promise<RollupTickResult> {
  const result: RollupTickResult = {
    eligible: 0,
    enqueued: 0,
    claimed: 0,
    skippedDeveloperMode: 0,
  };

  const owners = await distinctOwnersWithActivity(env, now, 20);
  for (const owner of owners) {
    if (await isDeveloperMode(env, owner.userId)) {
      result.skippedDeveloperMode += 1;
      continue;
    }
    const timezone = await getOwnerTimezone(env, owner.userId);
    const conversations = await listConversations(env, owner.userId);
    for (const conversation of conversations) {
      const periods = await completeRollupPeriods(
        env,
        conversation,
        timezone,
        now,
      );
      for (const period of periods) {
        result.eligible += 1;
        const created = await enqueueRollupJob(env, {
          jobId: `rollup:${owner.userId}:${conversation.id}:${period.start}`,
          ownerSub: owner.userId,
          conversationId: conversation.id,
          periodStart: period.start,
          periodEnd: period.end,
          inputSummaryIds: period.inputSummaryIds,
          inputSummaryVersions: period.inputSummaryVersions,
          now,
        });
        if (created) result.enqueued += 1;
      }
    }
  }

  for (let i = 0; i < CLAIMS_PER_TICK; i += 1) {
    const job = await claimSummaryScheduleJob(env, now, MAX_ATTEMPTS, LEASE_MS);
    if (!job) break;
    result.claimed += 1;
  }

  return result;
}

/** Find 30-day windows with complete daily coverage that lack a roll-up. */
async function completeRollupPeriods(
  env: Env,
  conversation: { id: string; owner_sub: string },
  timezone: string,
  now: number,
): Promise<{
  start: string;
  end: string;
  inputSummaryIds: string[];
  inputSummaryVersions: number[];
}[]> {
  const results: {
    start: string;
    end: string;
    inputSummaryIds: string[];
    inputSummaryVersions: number[];
  }[] = [];

  // Gather successful daily summaries for the last 60 days.
  const dailyRows = await recentDailySummaries(env, conversation.owner_sub, conversation.id);

  if (dailyRows.length < ROLLUP_DAYS) return results;

  // Sort by local_date ascending.
  dailyRows.sort((a, b) => a.local_date.localeCompare(b.local_date));

  // Slide a 30-day window across the available dates.
  const daySet = new Set(dailyRows.map((r) => r.local_date));
  const dates = Array.from(daySet).sort();

  for (let i = 0; i <= dates.length - ROLLUP_DAYS; i += 1) {
    const windowStart = dates[i];
    const windowEnd = dates[i + ROLLUP_DAYS - 1];

    // Verify every day in the window is present.
    let complete = true;
    let d = windowStart;
    for (let j = 0; j < ROLLUP_DAYS; j += 1) {
      if (!daySet.has(d)) {
        complete = false;
        break;
      }
      d = nextLocalDate(timezone, d);
    }
    if (!complete) continue;

    // Skip if a roll-up already exists or a job is pending for this period.
    if (await rollupAlreadyCompleted(env, conversation.owner_sub, conversation.id, windowStart)) continue;
    if (await rollupPeriodNeedsJob(env, conversation.owner_sub, conversation.id, windowStart)) continue;

    const windowRows = dailyRows.filter(
      (r) => r.local_date >= windowStart && r.local_date <= windowEnd,
    );
    results.push({
      start: windowStart,
      end: windowEnd,
      inputSummaryIds: windowRows.map((r) => r.id),
      inputSummaryVersions: windowRows.map((r) => r.version),
    });
  }

  return results;
}

async function recentDailySummaries(
  env: Env,
  ownerSub: string,
  conversationId: string,
): Promise<ChannelDailySummaryRow[]> {
  const { results } = await env.DB.prepare(
    `SELECT id, owner_sub, conversation_id, summary_type, period_start, period_end,
            local_date, content, version, covered_from_seq, covered_to_seq,
            source_summary_ids, index_status, index_error, retry_count,
            source_revision, created_at, updated_at, indexed_at
      FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2
        AND summary_type = 'daily' AND generation_status = 'success'
      ORDER BY local_date ASC`,
  )
    .bind(ownerSub, conversationId)
    .all<ChannelDailySummaryRow>();
  return results ?? [];
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

/** Advance a local date by one day in the given timezone. */
function nextLocalDate(timezone: string, date: string): string {
  const parts = date.split("-").map(Number);
  const ms = Date.UTC(parts[0], parts[1] - 1, parts[2], 12, 0, 0);
  const { endMs } = localDay(timezone, ms);
  const { date: nextDate } = localDay(timezone, endMs);
  return nextDate;
}

export { type RollupTickResult };
