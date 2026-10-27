import { apiFetch } from "../core";

/* ============================================================================
   Routines — the scheduled, recurring tasks the edge owns.

   A routine is created and updated through chat (the agent's routine tools);
   this module only reads the list and run history and toggles lifecycle, using
   the browser-facing routes the backend added.  Every call goes through
   ``apiFetch`` so the session cookie and CSRF header travel with it, exactly
   like every other authenticated request in the app.

   The backend answers ``available: false`` when the edge is not configured on
   this deployment, which is a different thing from "you have no routines"; the
   caller is expected to keep those two states distinct in the UI.
   ========================================================================== */

export interface Routine {
  routine_id: string;
  owner_sub: string;
  owner_id: string;
  agent_id: string;
  name: string;
  task_prompt: string;
  schedule_type: string;
  timezone: string;
  /** Human-readable schedule, computed at the edge (e.g. "every day at 8:00"). */
  schedule_text: string;
  /** 1 while the routine is active, 0 once paused or spent. */
  enabled: number;
  next_run_at: number | null;
  last_run_at: number | null;
  last_run_status: string;
  created_at: number;
  updated_at: number;
}

export interface RoutineRun {
  routine_run_id: string;
  routine_id: string;
  scheduled_for: number;
  /** pending | queued | running | delayed | completed | failed | rejected */
  status: string;
  attempt_count: number;
  started_at: number | null;
  completed_at: number | null;
  result_summary: string;
  error_code: string;
}

export interface RoutinesResult {
  ok: boolean;
  /** False when routines are not configured here at all (not merely empty). */
  available: boolean;
  error: string;
  routines: Routine[];
}

export interface RoutineRunsResult {
  ok: boolean;
  available: boolean;
  error: string;
  runs: RoutineRun[];
}

export interface RoutineActionResult {
  ok: boolean;
  available: boolean;
  error: string;
  routine?: Routine;
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export async function fetchRoutines(): Promise<RoutinesResult> {
  try {
    const response = await apiFetch("/routines");
    const data = await readJson(response);
    return {
      ok: Boolean(data.ok),
      available: data.available !== false,
      error: typeof data.error === "string" ? data.error : "",
      routines: Array.isArray(data.routines) ? (data.routines as Routine[]) : [],
    };
  } catch {
    return { ok: false, available: true, error: "Could not reach the backend.", routines: [] };
  }
}

export async function fetchRoutineRuns(routineId: string): Promise<RoutineRunsResult> {
  try {
    const response = await apiFetch(`/routines/${encodeURIComponent(routineId)}/runs`);
    const data = await readJson(response);
    return {
      ok: Boolean(data.ok),
      available: data.available !== false,
      error: typeof data.error === "string" ? data.error : "",
      runs: Array.isArray(data.runs) ? (data.runs as RoutineRun[]) : [],
    };
  } catch {
    return { ok: false, available: true, error: "Could not reach the backend.", runs: [] };
  }
}

async function lifecycle(path: string, method: string): Promise<RoutineActionResult> {
  try {
    const response = await apiFetch(path, { method });
    const data = await readJson(response);
    return {
      ok: Boolean(data.ok),
      available: data.available !== false,
      error: typeof data.error === "string" ? data.error : "",
      routine: data.routine as Routine | undefined,
    };
  } catch {
    return { ok: false, available: true, error: "Could not reach the backend." };
  }
}

export const pauseRoutine = (id: string) =>
  lifecycle(`/routines/${encodeURIComponent(id)}/pause`, "POST");

export const resumeRoutine = (id: string) =>
  lifecycle(`/routines/${encodeURIComponent(id)}/resume`, "POST");

export const deleteRoutine = (id: string) =>
  lifecycle(`/routines/${encodeURIComponent(id)}`, "DELETE");

/* ============================================================================
   Connector approvals — pending Allow/Deny cards the owner must action before
   any connector (Gmail, Drive, ...) runs.  These are scoped to the session;
   the backend enforces ownership.
   ========================================================================== */

export interface ConnectorApproval {
  approval_id: string;
  connector: string;
  action: string;
  arguments: Record<string, unknown>;
  expires_at: number;
  created_at: number;
  routine_id: string;
  routine_run_id: string;
  status: string;
}

export interface ConnectorApprovalsResult {
  ok: boolean;
  error: string;
  approvals: ConnectorApproval[];
}

export async function fetchConnectorApprovals(): Promise<ConnectorApprovalsResult> {
  try {
    const response = await apiFetch("/routines/connectors/approvals");
    const data = await readJson(response);
    return {
      ok: Boolean(data.ok),
      error: typeof data.error === "string" ? data.error : "",
      approvals: Array.isArray(data.approvals) ? (data.approvals as ConnectorApproval[]) : [],
    };
  } catch {
    return { ok: false, error: "Could not reach the backend.", approvals: [] };
  }
}

export async function executeConnectorApproval(approvalId: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const response = await apiFetch("/routines/connectors/approvals/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approval_id: approvalId }),
    });
    const data = await readJson(response);
    return { ok: Boolean(data.ok), error: typeof data.error === "string" ? data.error : undefined };
  } catch {
    return { ok: false, error: "Could not reach the backend." };
  }
}

export async function denyConnectorApproval(approvalId: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const response = await apiFetch("/routines/connectors/approvals/deny", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approval_id: approvalId }),
    });
    const data = await readJson(response);
    return { ok: Boolean(data.ok), error: typeof data.error === "string" ? data.error : undefined };
  } catch {
    return { ok: false, error: "Could not reach the backend." };
  }
}

