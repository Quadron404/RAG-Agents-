import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import { createRoutine, getRoutine, listRuns } from "../src/routines";
import { runScheduler } from "../src/scheduler";
import type { FetchLike } from "../src/routine-execution";

const BASE = Date.parse("2024-05-01T00:00:00Z");
let seq = 0;

function withEnv(overrides: Partial<Env>): Env {
  return new Proxy(env, {
    get(target, key, receiver) {
      if (key in overrides) return overrides[key as keyof Env];
      return Reflect.get(target, key, receiver);
    },
  }) as Env;
}

const groqEnv = withEnv({ GROQ_API_KEY: "test-key" });

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

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

/** A stub Groq endpoint for the edge-executed routine runner. */
function groq(options: { status?: number; text?: string } = {}): FetchLike {
  return async (input) => {
    const url = String(input);
    if (!url.includes("api.groq.com") || !url.includes("/chat/completions")) {
      throw new Error(`unexpected ${url}`);
    }
    if (options.status && options.status >= 400) {
      return new Response(JSON.stringify({ error: { message: "groq error" } }), {
        status: options.status,
        headers: { "content-type": "application/json" },
      });
    }
    return json({ choices: [{ message: { content: options.text ?? "sent the digest" } }] });
  };
}

describe("runScheduler (edge execution)", () => {
  it("materialises and completes a due one-time routine, recording the real result", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const result = await runScheduler(groqEnv, BASE + 2000, { fetchImpl: groq({ text: "sent the digest" }) });

    expect(result.materialized).toBe(1);
    expect(result.executed).toBe(1);
    expect(result.completed).toBe(1);

    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("completed");
    expect(runs[0].result_summary).toBe("sent the digest");
    const after = await getRoutine(env, routine.routine_id);
    expect(after?.last_run_status).toBe("completed");
    expect(after?.enabled).toBe(0); // a spent one-time routine
  });

  it("is idempotent: a second tick starts no duplicate work", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const fetchImpl = groq({ text: "sent the digest" });
    await runScheduler(groqEnv, BASE + 2000, { fetchImpl });
    const second = await runScheduler(groqEnv, BASE + 2000, { fetchImpl });
    expect(second.materialized).toBe(0);
    expect(second.executed).toBe(0);
    expect(await listRuns(env, routine.routine_id)).toHaveLength(1);
  });

  it("delays a rate-limited run rather than failing or hot-retrying it", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const now = BASE + 2000;
    const result = await runScheduler(groqEnv, now, { fetchImpl: groq({ status: 429 }) });
    expect(result.delayed).toBe(1);
    expect(result.completed).toBe(0);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("delayed");
    expect(runs[0].available_at).toBe(now + 60_000); // ROUTINE_RETRY_BASE_MS
  });

  it("keeps summaries on the Worker and skips when nothing is due", async () => {
    const result = await runScheduler(groqEnv, BASE, { fetchImpl: groq() });
    expect(result.materialized).toBe(0);
    expect(result.executed).toBe(0);
    expect(result.summaries.summarized).toBe(0);
    expect(result.summaries.eligible).toBe(0);
  });
});
