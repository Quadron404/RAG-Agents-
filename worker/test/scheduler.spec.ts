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

describe("runScheduler (Worker-native edge execution)", () => {
  it("skips all work when nothing is due", async () => {
    const result = await runScheduler(env, BASE);
    expect(result.materialized).toBe(0);
    expect(result.executed).toBe(0);
    expect(result.completed).toBe(0);
    expect(typeof result.summaries).toBe("string");
  });

  it("materialises a due one-time routine exactly once across ticks", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const first = await runScheduler(env, BASE + 2000);
    const second = await runScheduler(env, BASE + 2000);
    expect(first.materialized).toBe(1);
    expect(second.materialized).toBe(0);
    expect(second.executed).toBe(0);
    expect(await listRuns(env, routine.routine_id)).toHaveLength(1);
  });

  it("fails a due run immediately when execution is non-retryable (no provider key configured)", async () => {
    // No GROQ_API_KEY is configured in the test env, so executeRoutine throws a
    // non-retryable configuration error.  Before the retryability fix this run
    // was delayed and retried until the attempt budget was spent; now it fails
    // on the first attempt.
    const routine = await onceRoutine(BASE + 1000);
    const result = await runScheduler(env, BASE + 2000);
    expect(result.materialized).toBe(1);
    expect(result.executed).toBe(1);
    expect(result.failed).toBe(1);
    expect(result.delayed).toBe(0);
    expect(result.completed).toBe(0);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("failed");
    expect(runs[0].error_code).toContain("Groq API key is not configured");
    const after = await getRoutine(env, routine.routine_id);
    expect(after?.last_run_status).toBe("failed");
  });
});
