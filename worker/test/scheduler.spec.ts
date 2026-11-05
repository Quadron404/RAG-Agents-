import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { createRoutine, getRoutine, listRuns } from "../src/routines";
import { runScheduler } from "../src/scheduler";

const BASE = Date.parse("2024-05-01T00:00:00Z");
let seq = 0;

async function onceRoutine(atMs: number) {
  seq += 1;
  const result = await createRoutine(
    env,
    {
      owner_sub: `sched-${seq}`,
      owner_id: "user-1",
      agent_id: "agent-1",
      name: "Fire once",
      task_prompt: "Do the thing",
      schedule_type: "once",
      schedule_definition: { at: new Date(atMs).toISOString() },
      timezone: "UTC",
    },
    BASE,
  );
  if (!result.ok) throw new Error(result.error);
  return result.routine;
}

describe("runScheduler", () => {
  it("materialises a due one-time routine into exactly one run", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const result = await runScheduler(env, BASE + 2000);
    expect(result.materialized).toBe(1);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs).toHaveLength(1);
    // No Groq key in the test env: execution is attempted then delayed
    // (retryable, attempt budget not yet spent) rather than failed outright.
    expect(runs[0].status).toBe("delayed");
    expect(runs[0].error_code).toBe("Groq API key is not configured on this Worker");
  });

  it("is idempotent: a second tick starts no duplicate work", async () => {
    const routine = await onceRoutine(BASE + 1000);
    await runScheduler(env, BASE + 2000);
    const second = await runScheduler(env, BASE + 2000);
    expect(second.materialized).toBe(0);
    expect(await listRuns(env, routine.routine_id)).toHaveLength(1);
    const after = await getRoutine(env, routine.routine_id);
    expect(after?.enabled).toBe(0); // a spent one-time routine
  });

  it("does not complete a run when execution cannot complete (no Groq key)", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const result = await runScheduler(env, BASE + 2000);
    expect(result.completed).toBe(0);
    expect(result.failed).toBe(0);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("delayed");
  });

  it("skips summaries when nothing is due", async () => {
    const result = await runScheduler(env, BASE);
    expect(result.materialized).toBe(0);
    expect(result.summaries).toEqual({ eligible: 0, summarized: 0, indexed: 0, failed: 0 });
  });
});
