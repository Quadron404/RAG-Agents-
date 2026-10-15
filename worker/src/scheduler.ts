/**
 * The durable scheduler: one tick, no loop.
 *
 * The Worker's Cron Trigger calls this.  It is the only thing that decides when
 * a routine runs, which is what keeps there from being a second scheduler
 * competing with it.  A tick does four things, in order:
 *
 *   1. **Materialise.**  Every routine whose cursor is due gets exactly one run
 *      row for that occurrence, and its cursor advances.  Durable before any
 *      dispatch -- a crash between here and step 3 leaves a run that a later
 *      tick picks up, never a lost one.
 *   2. **Decide whether the backend is needed.**  If there is routine work, the
 *      backend is woken and awaited.  If there is only quiet-conversation
 *      summary work, the backend is *not* woken: a CO2-cheap minute of idle is
 *      not worth a Codespace boot, so summaries ride on the next tick that has
 *      real work, or on a tick where the backend happens to already be up.
 *   3. **Dispatch.**  Each due run is leased (atomically), sent to the backend's
 *      ``/internal/routines/execute``, and recorded from the *real* answer: a
 *      run is only ``completed`` when the backend returned a result.
 *   4. **Summaries.**  Whenever the backend is up, one summaries tick runs,
 *      reusing the Phase-3 inactivity/high-watermark machinery.
 *
 * Failure handling is deliberately conservative: an unreachable backend leaves
 * runs queued; a provider rate limit delays them with backoff; a spent attempt
 * budget or a non-retryable error fails them, with the reason recorded.
 */
import type { Env } from "./env";
import { ensureBackendReady, probeBackend, type LifecycleDeps } from "./codespace";
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

export interface SchedulerDeps extends LifecycleDeps {}

export interface SchedulerResult {
  materialized: number;
  dispatched: number;
  completed: number;
  failed: number;
  delayed: number;
  rejected: number;
  queued: number;
  backendReady: boolean;
  summaries: "ran" | "skipped" | "failed";
  note: string;
}

function intFrom(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

//: How long a dispatched run is leased.  Long enough to cover a real agent run,
//: short enough that a Worker that dies mid-dispatch has its run retried.
const LEASE_MS = 5 * 60 * 1000;

export async function runScheduler(
  env: Env,
  nowMs: number,
  deps: SchedulerDeps = {},
): Promise<SchedulerResult> {
  const batch = intFrom(env.ROUTINE_DISPATCH_BATCH, 20);
  const result: SchedulerResult = {
    materialized: 0,
    dispatched: 0,
    completed: 0,
    failed: 0,
    delayed: 0,
    rejected: 0,
    queued: 0,
    backendReady: false,
    summaries: "skipped",
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

  // 2. Anything to dispatch?
  const runs = await dispatchableRuns(env, nowMs, batch);
  if (runs.length === 0) {
    // Only passive work could be pending.  Never wake the Codespace for it.
    const probe = await probeBackend(env, deps);
    result.backendReady = probe.up;
    if (probe.up) {
      result.summaries = await pingSummaries(env, deps);
    } else {
      result.note = `no routine work; summaries deferred (${probe.detail})`;
    }
    return result;
  }

  // 3. The backend must be up to dispatch ordinary tasks.
  const ready = await ensureBackendReady(env, { wake: true }, deps);
  result.backendReady = ready.ready;
  if (!ready.ready) {
    // Keep the runs durable and try again on a later tick.
    for (const run of runs) {
      if (run.status === "running") continue;
      await markQueued(env, run.routine_run_id, nowMs);
      result.queued += 1;
    }
    result.note = `backend not ready: ${ready.detail}`;
    return result;
  }

  // 4. Dispatch each run.
  for (const run of runs) {
    const outcome = await dispatchOne(env, run, nowMs, deps);
    result.dispatched += 1;
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
    }
  }

  // 5. Reuse the Phase-3 summary machinery now that the backend is awake.
  result.summaries = await pingSummaries(env, deps);
  return result;
}

type Outcome = "completed" | "failed" | "delayed" | "rejected" | "queued";

async function dispatchOne(
  env: Env,
  run: RunRow,
  nowMs: number,
  deps: SchedulerDeps,
): Promise<Outcome> {
  const routine = await getRoutine(env, run.routine_id);
  if (!routine) {
    // Deleted between materialise and dispatch: never start it.
    await finishRun(env, run.routine_run_id, "rejected", { error: "routine_inactive" }, nowMs);
    return "rejected";
  }
  // Note: a paused routine is *not* re-checked here.  Pausing already rejects
  // its not-yet-dispatched runs, so anything still dispatchable is legitimate --
  // and a one-time routine is switched off (enabled = 0) the moment its single
  // run is materialised, which must not stop that run from executing.

  const claimed = await claimRun(env, run.routine_run_id, nowMs, LEASE_MS);
  if (!claimed) {
    // Another dispatcher won the lease.  Nothing to do.
    return "queued";
  }

  const outcome = await dispatchToBackend(env, routine, claimed, deps);
  const attempt = claimed.attempt_count;
  const maxAttempts = intFrom(env.ROUTINE_MAX_ATTEMPTS, 5);

  if (outcome.kind === "completed") {
    await finishRun(
      env,
      run.routine_run_id,
      "completed",
      { result: outcome.result },
      nowMs,
    );
    await recordRoutineStatus(env, routine.routine_id, "completed", nowMs);
    return "completed";
  }

  if (outcome.kind === "rejected") {
    await finishRun(env, run.routine_run_id, "rejected", { error: outcome.error }, nowMs);
    return "rejected";
  }

  // Retryable: rate limit, transient 5xx, or unreachable.  Delay with backoff,
  // unless the attempt budget is spent.
  if (outcome.retryable && attempt < maxAttempts) {
    const base = intFrom(env.ROUTINE_RETRY_BASE_MS, 60000);
    const backoff = Math.min(base * 2 ** Math.max(0, attempt - 1), 60 * 60 * 1000);
    await delayRun(env, run.routine_run_id, { availableAt: nowMs + backoff, error: outcome.error }, nowMs);
    await recordRoutineStatus(env, routine.routine_id, "delayed", nowMs);
    return "delayed";
  }

  await finishRun(env, run.routine_run_id, "failed", { error: outcome.error }, nowMs);
  await recordRoutineStatus(env, routine.routine_id, "failed", nowMs);
  return "failed";
}

type DispatchResult =
  | { kind: "completed"; result: string }
  | { kind: "retryable"; error: string }
  | { kind: "rejected"; error: string }
  | { kind: "fatal"; error: string };

interface BackendAnswer {
  ok?: unknown;
  result?: unknown;
  summary?: unknown;
  error?: unknown;
  error_code?: unknown;
  retryable?: unknown;
}

async function dispatchToBackend(
  env: Env,
  routine: RoutineView,
  run: RunRow,
  deps: SchedulerDeps,
): Promise<DispatchResult & { retryable?: boolean }> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const origin = (env.BACKEND_ORIGIN || "").replace(/\/+$/, "");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetchImpl(`${origin}/internal/routines/execute`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-rag-internal-token": env.RAG_INTERNAL_TOKEN,
      },
      body: JSON.stringify({
        routine_id: routine.routine_id,
        routine_run_id: run.routine_run_id,
        idempotency_key: run.idempotency_key,
        owner_id: routine.owner_id,
        owner_sub: routine.owner_sub,
        agent_id: routine.agent_id,
        name: routine.name,
        task_prompt: routine.task_prompt,
        scheduled_for: run.scheduled_for,
      }),
      signal: controller.signal,
    });
    const text = await response.text();
    let body: BackendAnswer = {};
    try {
      body = JSON.parse(text) as BackendAnswer;
    } catch {
      body = {};
    }

    if (response.ok && body.ok === true) {
      const result = String(body.result ?? body.summary ?? "ok");
      return { kind: "completed", result };
    }

    const errorCode = String(body.error_code ?? body.error ?? `http ${response.status}`);
    // Rate limits and transient upstream failures are retryable; a request the
    // backend explicitly marked non-retryable is not.
    const retryable =
      response.status === 429 ||
      response.status >= 500 ||
      body.error_code === "rate_limited" ||
      body.error_code === "provider_unavailable" ||
      (body.retryable === true);
    if (body.retryable === false && response.status < 500 && response.status !== 429) {
      return { kind: "fatal", error: errorCode, retryable: false };
    }
    if (retryable) {
      return { kind: "retryable", error: errorCode, retryable: true };
    }
    return { kind: "fatal", error: errorCode, retryable: false };
  } catch (error) {
    return { kind: "retryable", error: `dispatch failed: ${(error as Error).message}`, retryable: true };
  } finally {
    clearTimeout(timer);
  }
}

async function pingSummaries(env: Env, deps: SchedulerDeps): Promise<"ran" | "skipped" | "failed"> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const origin = (env.BACKEND_ORIGIN || "").replace(/\/+$/, "");
  if (!origin) return "skipped";
  try {
    const response = await fetchImpl(`${origin}/internal/summaries/run`, {
      method: "POST",
      headers: { "x-rag-internal-token": env.RAG_INTERNAL_TOKEN },
    });
    return response.ok ? "ran" : "failed";
  } catch {
    return "failed";
  }
}
