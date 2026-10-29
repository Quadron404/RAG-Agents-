/**
 * The durable scheduler: one tick, no loop.
 *
 * The Worker's Cron Trigger calls this.  It is the only thing that decides when
 * a routine runs, which is what keeps there from being a second scheduler
 * competing with it.  A tick does four things, in order:
 *
 *   1. **Materialise.**  Every routine whose cursor is due gets exactly one run
 *      row for that occurrence, and its cursor advances.  Durable before any
 *      execution -- a crash between here and step 3 leaves a run that a later
 *      tick picks up, never a lost one.
 *   2. **Execute.**  Each due run is leased (atomically), executed directly at
 *      the edge using Groq GPT-OSS 20B, and recorded from the real answer: a
 *      run is only ``completed`` when execution returns a result.
 *   3. **Summaries.**  Worker-native summaries ticks run every minute,
 *      independent of routine execution.
 *
 * Failure handling is deliberately conservative: a provider rate limit delays
 * runs with backoff; a non-retryable error fails them with the reason recorded.
 * Routines never depend on the Codespace backend being awake.
 */
import type { Env } from "./env";
import { runSummaryTick } from "./chat/summaries";
import { runDailyScheduleTick, runDailySummaryTick, type DailySummaryTickResult, type ScheduleTickResult } from "./chat/daily-schedule";
import { runRollupScheduleTick, type RollupTickResult } from "./chat/rollup-schedule";
import { runRollupSummaryTick } from "./chat/daily-rollup";
import { scheduleMonthlyCompaction, runMonthlyCompactTick, type MonthlyCompactTickResult } from "./chat/monthly-compact";
import { scheduleYearlyCompaction, runYearlyCompactTick, type YearlyCompactTickResult } from "./chat/yearly-compact";
import { executeRoutine, type RoutineExecutionResult } from "./routine-execution";
import {
  advanceRoutine,
  claimRun,
  delayRun,
  dispatchableRuns,
  dueRoutines,
  finishRun,
  getRoutine,
  markQueued,
  materializeRun,
  recordRoutineStatus,
  type RoutineView,
} from "./routines";
import type { RunRow } from "./routines-types";

export interface SchedulerResult {
  materialized: number;
  executed: number;
  completed: number;
  failed: number;
  delayed: number;
  rejected: number;
  queued: number;
  summaries: "ran" | "skipped" | "failed";
  dailySchedule: ScheduleTickResult | null;
  rollupSchedule: RollupTickResult | null;
  rollupGenerated: number;
  dailySummary: DailySummaryTickResult | null;
  monthlyCompact: MonthlyCompactTickResult | null;
  yearlyCompact: YearlyCompactTickResult | null;
  note: string;
}

function intFrom(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

//: How long an executed run is leased.  Long enough to cover a real agent run,
//: short enough that a Worker that dies mid-execution has its run retried.
const LEASE_MS = 5 * 60 * 1000;

export async function runScheduler(
  env: Env,
  nowMs: number,
): Promise<SchedulerResult> {
  const batch = intFrom(env.ROUTINE_DISPATCH_BATCH, 20);
  const result: SchedulerResult = {
    materialized: 0,
    executed: 0,
    completed: 0,
    failed: 0,
    delayed: 0,
    rejected: 0,
    queued: 0,
    summaries: "skipped",
    dailySchedule: null,
    rollupSchedule: null,
    rollupGenerated: 0,
    dailySummary: null,
    monthlyCompact: null,
    yearlyCompact: null,
    note: "",
  };

  // 1. Materialise due occurrences.
  const due = await dueRoutines(env, nowMs, batch);
  for (const routine of due) {
    const scheduledFor = routine.next_run_at as number;
    await materializeRun(env, routine, scheduledFor, nowMs);
    await advanceRoutine(env, routine, scheduledFor, nowMs);
    result.materialized += 1;
  }

  // 2. Anything to execute?
  const runs = await dispatchableRuns(env, nowMs, batch);
  if (runs.length === 0) {
    // Only passive work could be pending.  Never wake the Codespace for it.
    result.summaries = await runSummaries(env, nowMs);
    result.dailySchedule = await runDailyScheduleTick(env, nowMs);
    result.dailySummary = await runDailySummaryTick(env, nowMs);
    result.rollupSchedule = await runRollupScheduleTick(env, nowMs);
    result.rollupGenerated = await runRollupSummaryTick(env, nowMs);
    result.monthlyCompact = await scheduleMonthlyCompaction(env, nowMs);
    result.monthlyCompact = await runMonthlyCompactTick(env, nowMs);
    result.yearlyCompact = await scheduleYearlyCompaction(env, nowMs);
    result.yearlyCompact = await runYearlyCompactTick(env, nowMs);
    result.note = `no routine work; ${result.summaries} summaries ran`;
    return result;
  }

  // 3. Execute each run directly at the edge.
  for (const run of runs) {
    const outcome = await executeOne(env, run, nowMs);
    result.executed += 1;
    switch (outcome) {
      case "completed":
        result.completed += 1;
        break;
      case "failed":
        result.failed += 1;
        break;
      case "delayed":
        result.delayed += 1;
        break;
      case "rejected":
        result.rejected += 1;
        break;
      case "queued":
        result.queued += 1;
        break;
      case "awaiting_approval":
        // Run is waiting for owner approval; not counted as completed/failed.
        break;
    }
  }

  // 4. Summaries are Worker-native and do not depend on the Codespace.
  result.summaries = await runSummaries(env, nowMs);
  result.dailySchedule = await runDailyScheduleTick(env, nowMs);
  result.dailySummary = await runDailySummaryTick(env, nowMs);
  result.rollupSchedule = await runRollupScheduleTick(env, nowMs);
  result.rollupGenerated = await runRollupSummaryTick(env, nowMs);
  result.monthlyCompact = await scheduleMonthlyCompaction(env, nowMs);
  result.monthlyCompact = await runMonthlyCompactTick(env, nowMs);
  result.yearlyCompact = await scheduleYearlyCompaction(env, nowMs);
  result.yearlyCompact = await runYearlyCompactTick(env, nowMs);
  return result;
}

type Outcome = "completed" | "failed" | "delayed" | "rejected" | "queued" | "awaiting_approval";

async function executeOne(
  env: Env,
  run: RunRow,
  nowMs: number,
): Promise<Outcome> {
  const routine = await getRoutine(env, run.routine_id);
  if (!routine) {
    // Deleted between materialise and execution: never start it.
    await finishRun(env, run.routine_run_id, "rejected", { error: "routine_inactive" }, nowMs);
    return "rejected";
  }

  const claimed = await claimRun(env, run.routine_run_id, nowMs, LEASE_MS);
  if (!claimed) {
    // Another isolate won the lease.  Nothing to do.
    return "queued";
  }

  try {
    const result: RoutineExecutionResult = await executeRoutine(env, routine.task_prompt, {
      routineRunId: run.routine_run_id,
      ownerSub: routine.owner_sub,
      ownerId: routine.owner_id,
    });

    // If execution created a pending approval (e.g. gmail.send_email), the run
    // waits for the owner to Allow/Deny.  It is not retried by the scheduler;
    // the approval handler resolves it when the owner decides.
    if (result.approvalId) {
      await env.DB.prepare(
        `UPDATE routine_runs
          SET status = 'awaiting_approval', approval_id = ?1, updated_at = ?2
        WHERE routine_run_id = ?3`,
      )
        .bind(result.approvalId, nowMs, run.routine_run_id)
        .run();
      await recordRoutineStatus(env, routine.routine_id, "awaiting_approval", nowMs);
      return "awaiting_approval";
    }

    await finishRun(
      env,
      run.routine_run_id,
      "completed",
      { result: result.text },
      nowMs,
    );
    await recordRoutineStatus(env, routine.routine_id, "completed", nowMs);
    return "completed";
  } catch (error) {
    const errMsg = (error as Error).message;
    const attempt = claimed.attempt_count;
    const maxAttempts = intFrom(env.ROUTINE_MAX_ATTEMPTS, 5);

    // Retryable failures are delayed with backoff unless the attempt budget is spent.
    const isRetryable = attempt < maxAttempts;
    if (isRetryable) {
      const base = intFrom(env.ROUTINE_RETRY_BASE_MS, 60000);
      const backoff = Math.min(base * 2 ** Math.max(0, attempt - 1), 60 * 60 * 1000);
      await delayRun(env, run.routine_run_id, { availableAt: nowMs + backoff, error: errMsg }, nowMs);
      await recordRoutineStatus(env, routine.routine_id, "delayed", nowMs);
      return "delayed";
    }

    await finishRun(env, run.routine_run_id, "failed", { error: errMsg }, nowMs);
    await recordRoutineStatus(env, routine.routine_id, "failed", nowMs);
    return "failed";
  }
}
