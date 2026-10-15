/**
 * Shared routine types.
 *
 * Kept in their own module so the recurrence engine, the D1 store and the
 * scheduler can all name the same shapes without importing each other (and
 * without a circular import between recurrence and the store).
 */

export type ScheduleType = "once" | "daily" | "weekly" | "monthly";

export type RoutineSchedule =
  | { type: "once"; at: string }
  | { type: "daily"; time: string }
  | { type: "weekly"; time: string; weekdays: number[] }
  | { type: "monthly"; time: string; day: number };

/**
 * The lifecycle of a run.  The allowed transitions are enforced in the store,
 * not left to callers:
 *
 *   pending -> running -> completed | failed | delayed
 *   pending | queued -> rejected        (routine paused/deleted first)
 *   delayed -> running -> ...
 *
 * ``queued`` is the durable "the Codespace was asleep" state: the run is real
 * and will be dispatched on a later tick, not lost.
 */
export type RunStatus =
  | "pending"
  | "queued"
  | "running"
  | "delayed"
  | "completed"
  | "failed"
  | "rejected";

export interface RoutineRow {
  routine_id: string;
  owner_sub: string;
  owner_id: string;
  agent_id: string;
  name: string;
  task_prompt: string;
  schedule_type: ScheduleType;
  schedule_definition: string;
  timezone: string;
  enabled: number;
  next_run_at: number | null;
  last_run_at: number | null;
  last_run_status: string;
  created_at: number | null;
  updated_at: number | null;
}

export interface RunRow {
  routine_run_id: string;
  routine_id: string;
  owner_sub: string;
  scheduled_for: number;
  status: RunStatus;
  attempt_count: number;
  lease_until: number | null;
  available_at: number | null;
  started_at: number | null;
  completed_at: number | null;
  result_summary: string;
  error_code: string;
  idempotency_key: string;
  approval_id: string;
  created_at: number | null;
  updated_at: number | null;
}
