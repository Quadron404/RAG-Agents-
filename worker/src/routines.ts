/**
 * D1 accessors for durable routines and their run ledger.
 *
 * Every state transition the scheduler depends on is expressed as a single
 * conditional UPDATE so it is atomic in D1 and safe to retry:
 *
 *   * ``materializeRun`` inserts with INSERT OR IGNORE on a unique idempotency
 *     key, so the same scheduled occurrence can never become two runs;
 *   * ``claimRun`` only moves a run to ``running`` when it is still claimable
 *     (pending/queued/delayed, or running with an expired lease), so two
 *     isolates racing on one tick cannot both dispatch it;
 *   * ``finishRun`` is the only way a run reaches ``completed``, and it is only
 *     called with a result the backend actually returned.
 */
import type { Env } from "./env";
import { describeSchedule, nextOccurrence, validateSchedule } from "./recurrence";
import type { RoutineRow, RoutineSchedule, RunRow, RunStatus } from "./routines-types";

function newId(): string {
  return crypto.randomUUID();
}

export interface CreateRoutineInput {
  owner_sub: string;
  owner_id: string;
  agent_id: string;
  name: string;
  task_prompt: string;
  schedule_type: string;
  schedule_definition: unknown;
  timezone: string;
}

export interface RoutineView extends RoutineRow {
  /** Parsed schedule, so callers do not each re-parse the JSON column. */
  schedule: RoutineSchedule | null;
  schedule_text: string;
}

const ROUTINE_COLUMNS =
  "routine_id, owner_sub, owner_id, agent_id, name, task_prompt, schedule_type, schedule_definition, " +
  "timezone, enabled, next_run_at, last_run_at, last_run_status, created_at, updated_at";

const RUN_COLUMNS =
  "routine_run_id, routine_id, owner_sub, scheduled_for, status, attempt_count, lease_until, available_at, " +
  "started_at, completed_at, result_summary, error_code, idempotency_key, approval_id, created_at, updated_at";

function view(row: RoutineRow): RoutineView {
  const schedule = parseDefinition(row);
  return {
    ...row,
    schedule,
    schedule_text: schedule ? describeSchedule(schedule, row.timezone) : "",
  };
}

function parseDefinition(row: RoutineRow): RoutineSchedule | null {
  try {
    const value = JSON.parse(row.schedule_definition || "{}");
    if (value && typeof value === "object" && typeof (value as { type?: unknown }).type === "string") {
      return value as RoutineSchedule;
    }
  } catch {
    // fall through to null
  }
  return null;
}

// --- create / read -------------------------------------------------------

export async function createRoutine(
  env: Env,
  input: CreateRoutineInput,
  nowMs: number,
): Promise<{ ok: true; routine: RoutineView } | { ok: false; error: string; ambiguous: boolean }> {
  const checked = validateSchedule(input.schedule_type, input.schedule_definition, input.timezone);
  if (!checked.ok) {
    return { ok: false, error: checked.error, ambiguous: checked.ambiguous };
  }
  const next = nextOccurrence(checked.schedule, checked.timeZone, nowMs);
  if (next === null) {
    return {
      ok: false,
      error:
        checked.schedule.type === "once"
          ? "that one-time moment is already in the past"
          : "that schedule never produces a future run",
      ambiguous: false,
    };
  }
  const routine_id = newId();
  const row: RoutineRow = {
    routine_id,
    owner_sub: input.owner_sub,
    owner_id: input.owner_id,
    agent_id: input.agent_id,
    name: input.name.slice(0, 200),
    task_prompt: input.task_prompt,
    schedule_type: checked.schedule.type,
    schedule_definition: JSON.stringify(checked.schedule),
    timezone: checked.timeZone,
    enabled: 1,
    next_run_at: next,
    last_run_at: null,
    last_run_status: "",
    created_at: nowMs,
    updated_at: nowMs,
  };
  await env.DB.prepare(
    `INSERT INTO routines (${ROUTINE_COLUMNS}) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)`,
  )
    .bind(
      row.routine_id,
      row.owner_sub,
      row.owner_id,
      row.agent_id,
      row.name,
      row.task_prompt,
      row.schedule_type,
      row.schedule_definition,
      row.timezone,
      row.enabled,
      row.next_run_at,
      row.last_run_at,
      row.last_run_status,
      row.created_at,
      row.updated_at,
    )
    .run();
  return { ok: true, routine: view(row) };
}

export async function getRoutine(env: Env, routineId: string): Promise<RoutineView | null> {
  const row = await env.DB.prepare(`SELECT ${ROUTINE_COLUMNS} FROM routines WHERE routine_id = ?1`)
    .bind(routineId)
    .first<RoutineRow>();
  return row ? view(row) : null;
}

export async function listRoutines(env: Env, ownerSub: string): Promise<RoutineView[]> {
  const { results } = await env.DB.prepare(
    `SELECT ${ROUTINE_COLUMNS} FROM routines WHERE owner_sub = ?1 ORDER BY created_at DESC`,
  )
    .bind(ownerSub)
    .all<RoutineRow>();
  return results.map(view);
}

export async function dueRoutines(env: Env, nowMs: number, limit = 50): Promise<RoutineView[]> {
  const { results } = await env.DB.prepare(
    `SELECT ${ROUTINE_COLUMNS} FROM routines
     WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?1
     ORDER BY next_run_at ASC LIMIT ?2`,
  )
    .bind(nowMs, limit)
    .all<RoutineRow>();
  return results.map(view);
}

// --- update / lifecycle --------------------------------------------------

export interface RoutinePatch {
  name?: string;
  task_prompt?: string;
  schedule_type?: string;
  schedule_definition?: unknown;
  timezone?: string;
}

export async function updateRoutine(
  env: Env,
  routineId: string,
  patch: RoutinePatch,
  nowMs: number,
): Promise<{ ok: true; routine: RoutineView } | { ok: false; error: string; ambiguous: boolean }> {
  const existing = await getRoutine(env, routineId);
  if (!existing) return { ok: false, error: "no such routine", ambiguous: false };

  const scheduleType = patch.schedule_type ?? existing.schedule_type;
  const definition = patch.schedule_definition ?? existing.schedule;
  const timeZone = patch.timezone ?? existing.timezone;
  const checked = validateSchedule(scheduleType, definition, timeZone);
  if (!checked.ok) return { ok: false, error: checked.error, ambiguous: checked.ambiguous };

  // Recompute from now so an edited schedule takes effect without waiting for
  // the old next_run_at to pass.
  const next = nextOccurrence(checked.schedule, checked.timeZone, nowMs);
  if (next === null) return { ok: false, error: "that schedule never produces a future run", ambiguous: false };

  const name = patch.name ?? existing.name;
  const taskPrompt = patch.task_prompt ?? existing.task_prompt;
  await env.DB.prepare(
    `UPDATE routines SET name = ?2, task_prompt = ?3, schedule_type = ?4, schedule_definition = ?5,
       timezone = ?6, next_run_at = ?7, updated_at = ?8 WHERE routine_id = ?1`,
  )
    .bind(
      routineId,
      name.slice(0, 200),
      taskPrompt,
      checked.schedule.type,
      JSON.stringify(checked.schedule),
      checked.timeZone,
      next,
      nowMs,
    )
    .run();
  const updated = await getRoutine(env, routineId);
  return { ok: true, routine: updated as RoutineView };
}

/**
 * Pause or resume a routine.
 *
 * Pausing also rejects its not-yet-dispatched runs, so "paused" means no work
 * starts: a run that is already executing is left alone (it will finish and
 * record its real result), but a pending one is closed rather than dispatched
 * later against the user's wishes.
 */
export async function setRoutineEnabled(
  env: Env,
  routineId: string,
  enabled: boolean,
  nowMs: number,
): Promise<RoutineView | null> {
  const existing = await getRoutine(env, routineId);
  if (!existing) return null;
  let nextRunAt = existing.next_run_at;
  if (enabled && existing.schedule) {
    const next = nextOccurrence(existing.schedule, existing.timezone, nowMs);
    nextRunAt = next;
  } else if (!enabled) {
    nextRunAt = null;
  }
  await env.DB.prepare(
    "UPDATE routines SET enabled = ?2, next_run_at = ?3, updated_at = ?4 WHERE routine_id = ?1",
  )
    .bind(routineId, enabled ? 1 : 0, nextRunAt, nowMs)
    .run();
  if (!enabled) {
    await rejectOpenRuns(env, routineId, "routine_paused", nowMs);
  }
  return getRoutine(env, routineId);
}

export async function deleteRoutine(env: Env, routineId: string, nowMs: number): Promise<boolean> {
  const existing = await getRoutine(env, routineId);
  if (!existing) return false;
  // Close any not-yet-dispatched run first (so a racing dispatcher sees the
  // rejection rather than a vanished row), then remove the routine's run ledger
  // with it: a run whose routine no longer exists is not inspectable anyway.
  await rejectOpenRuns(env, routineId, "routine_deleted", nowMs);
  await env.DB.prepare("DELETE FROM routine_runs WHERE routine_id = ?1").bind(routineId).run();
  await env.DB.prepare("DELETE FROM routines WHERE routine_id = ?1").bind(routineId).run();
  return true;
}

async function rejectOpenRuns(env: Env, routineId: string, reason: string, nowMs: number): Promise<void> {
  await env.DB.prepare(
    `UPDATE routine_runs SET status = 'rejected', error_code = ?2, completed_at = ?3, updated_at = ?3,
       lease_until = NULL
     WHERE routine_id = ?1 AND status IN ('pending', 'queued', 'delayed')`,
  )
    .bind(routineId, reason, nowMs)
    .run();
}

// --- runs ----------------------------------------------------------------

export function idempotencyKey(routineId: string, scheduledFor: number): string {
  return `${routineId}:${scheduledFor}`;
}

/**
 * Create (or return the existing) run for one scheduled occurrence.
 *
 * Idempotent by the unique idempotency key.  The INSERT OR IGNORE followed by a
 * SELECT means a retried tick, a Worker restart or two racing isolates all end
 * up describing the same single run.
 */
export async function materializeRun(
  env: Env,
  routine: RoutineView,
  scheduledFor: number,
  nowMs: number,
): Promise<RunRow> {
  const key = idempotencyKey(routine.routine_id, scheduledFor);
  await env.DB.prepare(
    `INSERT OR IGNORE INTO routine_runs
       (routine_run_id, routine_id, owner_sub, scheduled_for, status, attempt_count, available_at,
        idempotency_key, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, 'pending', 0, ?5, ?6, ?7, ?7)`,
  )
    .bind(newId(), routine.routine_id, routine.owner_sub, scheduledFor, scheduledFor, key, nowMs)
    .run();
  const row = await env.DB.prepare(`SELECT ${RUN_COLUMNS} FROM routine_runs WHERE idempotency_key = ?1`)
    .bind(key)
    .first<RunRow>();
  return row as RunRow;
}

/**
 * Advance a routine's cursor to the occurrence after the one just materialised.
 *
 * A one-time routine is switched off and its cursor cleared: the schedule is
 * spent, and leaving a past next_run_at behind would make it look "due" on
 * every tick forever.
 */
export async function advanceRoutine(
  env: Env,
  routine: RoutineView,
  scheduledFor: number,
  nowMs: number,
): Promise<void> {
  let nextRunAt: number | null = null;
  let enabled = routine.enabled;
  if (routine.schedule) {
    nextRunAt = nextOccurrence(routine.schedule, routine.timezone, scheduledFor);
    if (routine.schedule.type === "once") {
      enabled = 0;
    }
  }
  await env.DB.prepare(
    "UPDATE routines SET next_run_at = ?2, enabled = ?3, last_run_at = ?4, updated_at = ?5 WHERE routine_id = ?1",
  )
    .bind(routine.routine_id, nextRunAt, enabled, scheduledFor, nowMs)
    .run();
}

export async function recordRoutineStatus(env: Env, routineId: string, status: string, nowMs: number): Promise<void> {
  await env.DB.prepare("UPDATE routines SET last_run_status = ?2, updated_at = ?3 WHERE routine_id = ?1")
    .bind(routineId, status, nowMs)
    .run();
}

/**
 * Runs the dispatcher may pick up now: pending/queued/delayed that are due, plus
 * running runs whose lease has expired (the Worker that was dispatching them
 * died mid-flight and the run must be recoverable).
 */
export async function dispatchableRuns(env: Env, nowMs: number, limit = 20): Promise<RunRow[]> {
  const { results } = await env.DB.prepare(
    `SELECT ${RUN_COLUMNS} FROM routine_runs
     WHERE status IN ('pending', 'queued', 'delayed', 'running')
       AND (available_at IS NULL OR available_at <= ?1)
       AND (lease_until IS NULL OR lease_until <= ?1)
     ORDER BY scheduled_for ASC LIMIT ?2`,
  )
    .bind(nowMs, limit)
    .all<RunRow>();
  return results;
}

/**
 * Lease a run to this dispatcher.  The conditional WHERE is the whole point:
 * only a still-claimable row moves to ``running``, so a run cannot be picked up
 * twice, and attempt_count is incremented in the same statement that wins it.
 */
export async function claimRun(
  env: Env,
  runId: string,
  nowMs: number,
  leaseMs: number,
): Promise<RunRow | null> {
  return env.DB.prepare(
    `UPDATE routine_runs
     SET status = 'running', attempt_count = attempt_count + 1,
         started_at = COALESCE(started_at, ?2), lease_until = ?3, updated_at = ?2
     WHERE routine_run_id = ?1
       AND status IN ('pending', 'queued', 'delayed', 'running')
       AND (lease_until IS NULL OR lease_until <= ?2)
     RETURNING ${RUN_COLUMNS}`,
  )
    .bind(runId, nowMs, nowMs + leaseMs)
    .first<RunRow>();
}

/** Move a run that could not be dispatched back to ``queued`` for a later tick. */
export async function releaseRun(env: Env, runId: string, nowMs: number): Promise<void> {
  await env.DB.prepare(
    `UPDATE routine_runs SET status = 'queued', lease_until = NULL, updated_at = ?2
     WHERE routine_run_id = ?1 AND status = 'running'`,
  )
    .bind(runId, nowMs)
    .run();
}

/**
 * Mark a not-yet-dispatched run as ``queued``: the backend was not ready, so it
 * is durable and waiting rather than lost or failed.
 */
export async function markQueued(env: Env, runId: string, nowMs: number): Promise<void> {
  await env.DB.prepare(
    `UPDATE routine_runs SET status = 'queued', lease_until = NULL, updated_at = ?2
     WHERE routine_run_id = ?1 AND status IN ('pending', 'delayed')`,
  )
    .bind(runId, nowMs)
    .run();
}

export async function finishRun(
  env: Env,
  runId: string,
  status: Extract<RunStatus, "completed" | "failed" | "rejected">,
  options: { result?: string; error?: string },
  nowMs: number,
): Promise<void> {
  await env.DB.prepare(
    `UPDATE routine_runs SET status = ?2, result_summary = ?3, error_code = ?4, completed_at = ?5,
       lease_until = NULL, updated_at = ?5 WHERE routine_run_id = ?1`,
  )
    .bind(runId, status, (options.result ?? "").slice(0, 2000), options.error ?? "", nowMs)
    .run();
}

export async function delayRun(
  env: Env,
  runId: string,
  options: { availableAt: number; error?: string },
  nowMs: number,
): Promise<void> {
  await env.DB.prepare(
    `UPDATE routine_runs SET status = 'delayed', available_at = ?2, error_code = ?3,
       lease_until = NULL, updated_at = ?4 WHERE routine_run_id = ?1`,
  )
    .bind(runId, options.availableAt, options.error ?? "", nowMs)
    .run();
}

export async function listRuns(env: Env, routineId: string, limit = 20): Promise<RunRow[]> {
  const { results } = await env.DB.prepare(
    `SELECT ${RUN_COLUMNS} FROM routine_runs WHERE routine_id = ?1 ORDER BY scheduled_for DESC LIMIT ?2`,
  )
    .bind(routineId, limit)
    .all<RunRow>();
  return results;
}
