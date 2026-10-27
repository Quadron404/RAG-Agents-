/**
 * D1 accessors for daily-summary scheduling job state.
 *
 * One job per (owner, channel, local date).  Every state transition is a single
 * conditional UPDATE so it is atomic in D1 and safe to retry:
 *
 *   * enqueueSummaryScheduleJob inserts with INSERT OR IGNORE on the unique
 *     index, so the same (owner, channel, date) can never become two jobs;
 *   * claimSummaryScheduleJob only moves a job to 'claimed' when it is still
 *     claimable (pending, or claimed with an expired lease), so two isolates
 *     racing on one tick cannot both dispatch it;
 *   * markSummaryScheduleJob records the terminal state.
 */
import type { Env } from "../env";

export interface SummaryScheduleJobRow {
  job_id: string;
  owner_sub: string;
  conversation_id: string;
  local_date: string;
  summary_type: string;
  status: string;
  attempts: number;
  available_at: number;
  lease_until: number | null;
  error_code: string;
  created_at: number;
  updated_at: number;
  period_start: string;
  period_end: string;
  input_summary_ids: string;
  input_summary_versions: string;
}

const COLUMNS =
  "job_id, owner_sub, conversation_id, local_date, summary_type, status, attempts, " +
  "available_at, lease_until, error_code, created_at, updated_at, " +
  "period_start, period_end, input_summary_ids, input_summary_versions";

export async function channelDailySummaryExists(
  env: Env,
  ownerSub: string,
  conversationId: string,
  localDate: string,
): Promise<boolean> {
  const row = await env.DB.prepare(
    `SELECT 1 FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2 AND local_date = ?3
        AND summary_type = 'daily' AND generation_status = 'success'
      LIMIT 1`,
  )
    .bind(ownerSub, conversationId, localDate)
    .first<{ dummy: number }>();
  return !!row;
}

export async function enqueueSummaryScheduleJob(
  env: Env,
  value: {
    jobId: string;
    ownerSub: string;
    conversationId: string;
    localDate: string;
    now: number;
    resetFailed?: boolean;
  },
): Promise<boolean> {
  const now = value.now;
  await env.DB.prepare(
    `INSERT INTO summary_schedule_jobs
       (job_id, owner_sub, conversation_id, local_date, summary_type, status,
        attempts, available_at, lease_until, error_code, created_at, updated_at)
      VALUES (?1, ?2, ?3, ?4, 'daily', 'pending', 0, ?5, NULL, '', ?5, ?5)
      ON CONFLICT(job_id) DO UPDATE SET
        status = CASE
          WHEN ?6 = 1 AND summary_schedule_jobs.status = 'failed' THEN 'pending'
          ELSE summary_schedule_jobs.status
        END,
        available_at = CASE
          WHEN ?6 = 1 AND summary_schedule_jobs.status = 'failed' THEN excluded.available_at
          ELSE summary_schedule_jobs.available_at
        END,
        error_code = '',
        updated_at = ?5`,
  )
    .bind(
      value.jobId,
      value.ownerSub,
      value.conversationId,
      value.localDate,
      now,
      value.resetFailed ? 1 : 0,
    )
    .run();
  return true;
}

export async function isDailySummaryDue(
  env: Env,
  ownerSub: string,
  conversationId: string,
  localDate: string,
): Promise<boolean> {
  const row = await env.DB.prepare(
    `SELECT 1 FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2 AND local_date = ?3
        AND summary_type = 'daily' AND generation_status = 'success'
      LIMIT 1`,
  )
    .bind(ownerSub, conversationId, localDate)
    .first<{ dummy: number }>();
  return !!row;
}

export async function claimSummaryScheduleJob(
  env: Env,
  now: number,
  maxAttempts: number,
  leaseMs: number,
): Promise<SummaryScheduleJobRow | null> {
  const candidate = await env.DB.prepare(
    `SELECT ${COLUMNS} FROM summary_schedule_jobs
      WHERE attempts < ?2
        AND (
          (status = 'pending' AND available_at <= ?1)
          OR (status = 'claimed' AND lease_until IS NOT NULL AND lease_until < ?1)
        )
      ORDER BY available_at ASC, created_at ASC
      LIMIT 1`,
  )
    .bind(now, maxAttempts)
    .first<SummaryScheduleJobRow>();
  if (!candidate) return null;
  const claimed = await env.DB.prepare(
    `UPDATE summary_schedule_jobs
      SET status = 'claimed', attempts = attempts + 1, lease_until = ?4, updated_at = ?1
      WHERE job_id = ?2 AND (
        (status = 'pending' AND available_at <= ?1)
        OR (status = 'claimed' AND lease_until IS NOT NULL AND lease_until < ?1)
      )
      RETURNING ${COLUMNS}`,
  )
    .bind(now, candidate.job_id, now, now + leaseMs)
    .first<SummaryScheduleJobRow>();
  return claimed ?? null;
}

export async function claimSummaryScheduleJobsByType(
  env: Env,
  now: number,
  summaryType: string,
  maxAttempts: number,
  leaseMs: number,
  limit = 10,
): Promise<SummaryScheduleJobRow[]> {
  const candidates = await env.DB.prepare(
    `SELECT ${COLUMNS} FROM summary_schedule_jobs
      WHERE summary_type = ?3 AND attempts < ?2
        AND (
          (status = 'pending' AND available_at <= ?1)
          OR (status = 'claimed' AND lease_until IS NOT NULL AND lease_until < ?1)
        )
      ORDER BY available_at ASC, created_at ASC
      LIMIT ?4`,
  )
    .bind(now, maxAttempts, summaryType, limit)
    .all<SummaryScheduleJobRow>();
  const claimed: SummaryScheduleJobRow[] = [];
  for (const candidate of candidates.results ?? []) {
    const row = await env.DB.prepare(
      `UPDATE summary_schedule_jobs
        SET status = 'claimed', attempts = attempts + 1, lease_until = ?5, updated_at = ?1
        WHERE job_id = ?2 AND (
          (status = 'pending' AND available_at <= ?1)
          OR (status = 'claimed' AND lease_until IS NOT NULL AND lease_until < ?1)
        )
        RETURNING ${COLUMNS}`,
    )
      .bind(now, candidate.job_id, now, now + leaseMs, now + leaseMs)
      .first<SummaryScheduleJobRow>();
    if (row) claimed.push(row);
  }
  return claimed;
}

export async function enqueueCompactionJob(
  env: Env,
  value: {
    jobId: string;
    ownerSub: string;
    conversationId: string;
    localDate: string;
    summaryType: string;
    periodStart: string;
    periodEnd: string;
    inputSummaryIds: string[];
    inputSummaryVersions: number[];
    now: number;
  },
): Promise<boolean> {
  const now = value.now;
  await env.DB.prepare(
    `INSERT INTO summary_schedule_jobs
       (job_id, owner_sub, conversation_id, local_date, summary_type, status,
        attempts, available_at, lease_until, error_code, created_at, updated_at,
        period_start, period_end, input_summary_ids, input_summary_versions)
     VALUES (?1, ?2, ?3, ?4, ?5, 'pending', 0, ?6, NULL, '', ?6, ?6,
       ?7, ?8, ?9, ?10)
     ON CONFLICT(job_id) DO UPDATE SET
       status = CASE
         WHEN summary_schedule_jobs.status = 'failed' THEN 'pending'
         ELSE summary_schedule_jobs.status
       END,
       period_start = excluded.period_start,
       period_end = excluded.period_end,
       input_summary_ids = excluded.input_summary_ids,
       input_summary_versions = excluded.input_summary_versions,
       updated_at = excluded.updated_at`,
  )
    .bind(
      value.jobId,
      value.ownerSub,
      value.conversationId,
      value.localDate,
      value.summaryType,
      now,
      value.periodStart,
      value.periodEnd,
      JSON.stringify(value.inputSummaryIds),
      JSON.stringify(value.inputSummaryVersions),
    )
    .run();
  return true;
}

export async function markSummaryScheduleJob(
  env: Env,
  jobId: string,
  status: "pending" | "claimed" | "completed" | "failed",
  options: { availableAt?: number; error?: string; now?: number } = {},
): Promise<void> {
  const now = options.now ?? Date.now();
  await env.DB.prepare(
    `UPDATE summary_schedule_jobs
      SET status = ?2, available_at = COALESCE(?3, available_at),
          error_code = COALESCE(?4, error_code), lease_until = NULL, updated_at = ?5
      WHERE job_id = ?1`,
  )
    .bind(jobId, status, options.availableAt ?? null, options.error ?? "", now)
    .run();
}

export async function listSummaryScheduleJobs(
  env: Env,
  ownerSub: string,
  limit = 20,
): Promise<SummaryScheduleJobRow[]> {
  const { results } = await env.DB.prepare(
    `SELECT ${COLUMNS} FROM summary_schedule_jobs
      WHERE owner_sub = ?1
      ORDER BY updated_at DESC, created_at DESC
      LIMIT ?2`,
  )
    .bind(ownerSub, limit)
    .all<SummaryScheduleJobRow>();
  return results ?? [];
}

export async function rollupPeriodNeedsJob(
  env: Env,
  ownerSub: string,
  conversationId: string,
  periodStart: string,
): Promise<boolean> {
  const row = await env.DB.prepare(
    `SELECT 1 FROM summary_schedule_jobs
      WHERE owner_sub = ?1 AND conversation_id = ?2
        AND summary_type = 'rollup_30d' AND period_start = ?3
        AND status IN ('pending', 'claimed')
      LIMIT 1`,
  )
    .bind(ownerSub, conversationId, periodStart)
    .first<{ dummy: number }>();
  return !!row;
}

export async function rollupAlreadyCompleted(
  env: Env,
  ownerSub: string,
  conversationId: string,
  periodStart: string,
): Promise<boolean> {
  const row = await env.DB.prepare(
    `SELECT 1 FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2
        AND summary_type = 'rollup_30d' AND period_start = ?3
        AND generation_status = 'success'
      LIMIT 1`,
  )
    .bind(ownerSub, conversationId, periodStart)
    .first<{ dummy: number }>();
  return !!row;
}

export async function enqueueRollupJob(
  env: Env,
  value: {
    jobId: string;
    ownerSub: string;
    conversationId: string;
    periodStart: string;
    periodEnd: string;
    inputSummaryIds: string[];
    inputSummaryVersions: number[];
    now: number;
  },
): Promise<boolean> {
  const now = value.now;
  await env.DB.prepare(
    `INSERT INTO summary_schedule_jobs
       (job_id, owner_sub, conversation_id, local_date, summary_type, status,
        attempts, available_at, lease_until, error_code, created_at, updated_at,
        period_start, period_end, input_summary_ids, input_summary_versions)
      VALUES (?1, ?2, ?3, ?4, 'rollup_30d', 'pending', 0, ?5, NULL, '', ?5, ?5,
        ?6, ?7, ?8, ?9)
      ON CONFLICT(job_id) DO UPDATE SET
        status = CASE
          WHEN summary_schedule_jobs.status = 'failed' THEN 'pending'
          ELSE summary_schedule_jobs.status
        END,
        period_start = excluded.period_start,
        period_end = excluded.period_end,
        input_summary_ids = excluded.input_summary_ids,
        input_summary_versions = excluded.input_summary_versions,
        updated_at = excluded.updated_at`,
  )
    .bind(
      value.jobId,
      value.ownerSub,
      value.conversationId,
      value.periodEnd, // local_date = end of period
      now,
      value.periodStart,
      value.periodEnd,
      JSON.stringify(value.inputSummaryIds),
      JSON.stringify(value.inputSummaryVersions),
    )
    .run();
  return true;
}
