import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import {
  claimRun,
  createRoutine,
  deleteRoutine,
  dispatchableRuns,
  dueRoutines,
  listRoutines,
  listRuns,
  materializeRun,
  setRoutineEnabled,
  updateRoutine,
} from "../src/routines";

let seq = 0;
function owner(): string {
  seq += 1;
  return `sub-${seq}`;
}

async function makeRoutine(sub: string, nowMs: number, overrides: Record<string, unknown> = {}) {
  const result = await createRoutine(
    env,
    {
      owner_sub: sub,
      owner_id: "user-1",
      agent_id: "agent-1",
      name: "Morning digest",
      task_prompt: "Summarise the inbox",
      schedule_type: "daily",
      schedule_definition: { time: "09:00" },
      timezone: "UTC",
      ...overrides,
    } as never,
    nowMs,
  );
  if (!result.ok) throw new Error(result.error);
  return result.routine;
}

describe("routine store", () => {
  it("creates a routine with a computed next run", async () => {
    const sub = owner();
    const now = Date.parse("2024-05-01T00:00:00Z");
    const routine = await makeRoutine(sub, now);
    expect(routine.enabled).toBe(1);
    expect(routine.schedule).toEqual({ type: "daily", time: "09:00" });
    expect(routine.next_run_at).toBe(Date.parse("2024-05-01T09:00:00Z"));
    expect(routine.schedule_text).toBe("every day at 09:00 (UTC)");
  });

  it("rejects an ambiguous schedule without writing a row", async () => {
    const sub = owner();
    const result = await createRoutine(
      env,
      {
        owner_sub: sub,
        owner_id: "u",
        agent_id: "a",
        name: "x",
        task_prompt: "y",
        schedule_type: "weekly",
        schedule_definition: { time: "09:00", weekdays: [] },
        timezone: "UTC",
      },
      Date.now(),
    );
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.ambiguous).toBe(true);
    expect(await listRoutines(env, sub)).toHaveLength(0);
  });

  it("materialises exactly one run per occurrence, however often it is asked", async () => {
    const sub = owner();
    const now = Date.parse("2024-05-01T00:00:00Z");
    const routine = await makeRoutine(sub, now);
    const first = await materializeRun(env, routine, routine.next_run_at as number, now);
    const second = await materializeRun(env, routine, routine.next_run_at as number, now);
    expect(second.routine_run_id).toBe(first.routine_run_id);
    expect(await listRuns(env, routine.routine_id)).toHaveLength(1);
  });

  it("leases a run once, then not again until the lease expires", async () => {
    const sub = owner();
    const now = Date.parse("2024-05-01T00:00:00Z");
    const routine = await makeRoutine(sub, now);
    const run = await materializeRun(env, routine, now, now);

    const first = await claimRun(env, run.routine_run_id, now, 60_000);
    expect(first?.status).toBe("running");
    expect(first?.attempt_count).toBe(1);

    const blocked = await claimRun(env, run.routine_run_id, now + 1000, 60_000);
    expect(blocked).toBeNull();

    const recovered = await claimRun(env, run.routine_run_id, now + 120_000, 60_000);
    expect(recovered?.status).toBe("running");
    expect(recovered?.attempt_count).toBe(2);
  });

  it("rejects not-yet-dispatched runs when paused, and starts none while paused", async () => {
    const sub = owner();
    const now = Date.parse("2024-05-01T00:00:00Z");
    const routine = await makeRoutine(sub, now);
    await materializeRun(env, routine, routine.next_run_at as number, now);

    const paused = await setRoutineEnabled(env, routine.routine_id, false, now);
    expect(paused?.enabled).toBe(0);
    expect(paused?.next_run_at).toBeNull();

    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("rejected");
    expect(runs[0].error_code).toBe("routine_paused");
    expect(await dueRoutines(env, Date.parse("2024-06-01T00:00:00Z"))).not.toContainEqual(
      expect.objectContaining({ routine_id: routine.routine_id }),
    );
  });

  it("recomputes the next run when the schedule is edited", async () => {
    const sub = owner();
    const now = Date.parse("2024-05-01T00:00:00Z");
    const routine = await makeRoutine(sub, now);
    const updated = await updateRoutine(env, routine.routine_id, { schedule_definition: { time: "18:00" } }, now);
    expect(updated.ok).toBe(true);
    if (updated.ok) expect(updated.routine.next_run_at).toBe(Date.parse("2024-05-01T18:00:00Z"));
  });

  it("surfaces due routines and dispatchable runs", async () => {
    const sub = owner();
    const now = Date.parse("2024-05-01T00:00:00Z");
    const routine = await makeRoutine(sub, now);
    const run = await materializeRun(env, routine, routine.next_run_at as number, now);
    const later = Date.parse("2024-05-01T10:00:00Z");
    expect((await dueRoutines(env, later)).some((r) => r.routine_id === routine.routine_id)).toBe(true);
    expect((await dispatchableRuns(env, later)).some((r) => r.routine_run_id === run.routine_run_id)).toBe(true);
  });

  it("deletes a routine and its open runs", async () => {
    const sub = owner();
    const now = Date.parse("2024-05-01T00:00:00Z");
    const routine = await makeRoutine(sub, now);
    await materializeRun(env, routine, routine.next_run_at as number, now);
    expect(await deleteRoutine(env, routine.routine_id, now)).toBe(true);
    expect(await listRuns(env, routine.routine_id)).toHaveLength(0);
    expect(await listRoutines(env, sub)).toHaveLength(0);
  });
});
