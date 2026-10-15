import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { createRoutine, getRoutine, listRuns } from "../src/routines";
import { runScheduler } from "../src/scheduler";

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const BASE = Date.parse("2024-05-01T00:00:00Z");
let seq = 0;

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

/** A stub backend whose /health, execute and summaries answers are configurable. */
function stubFetch(options: {
  healthUp: boolean;
  execute?: (body: Record<string, unknown>) => Response;
  summaries?: () => Response;
  seen?: Record<string, unknown>[];
}): FetchLike {
  return async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url.endsWith("/health")) {
      return options.healthUp ? json({ ok: true }) : new Response("", { status: 503 });
    }
    if (url.endsWith("/internal/routines/execute")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      options.seen?.push(body);
      return (options.execute ?? (() => json({ ok: true, result: "done" })))(body);
    }
    if (url.endsWith("/internal/summaries/run")) {
      return (options.summaries ?? (() => json({ ok: true })))();
    }
    throw new Error(`unexpected ${method} ${url}`);
  };
}

describe("runScheduler", () => {
  it("materialises and completes a due one-time routine, recording the real result", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const seen: Record<string, unknown>[] = [];
    const result = await runScheduler(env, BASE + 2000, {
      fetchImpl: stubFetch({
        healthUp: true,
        execute: () => json({ ok: true, result: "sent the digest" }),
        seen,
      }),
    });

    expect(result.materialized).toBe(1);
    expect(result.completed).toBe(1);
    expect(seen[0].idempotency_key).toBe(`${routine.routine_id}:${BASE + 1000}`);

    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("completed");
    expect(runs[0].result_summary).toBe("sent the digest");
    const after = await getRoutine(env, routine.routine_id);
    expect(after?.last_run_status).toBe("completed");
    expect(after?.enabled).toBe(0); // a spent one-time routine
  });

  it("is idempotent: a second tick starts no duplicate work", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const deps = { fetchImpl: stubFetch({ healthUp: true }) };
    await runScheduler(env, BASE + 2000, deps);
    const second = await runScheduler(env, BASE + 2000, deps);
    expect(second.materialized).toBe(0);
    expect(second.dispatched).toBe(0);
    expect(await listRuns(env, routine.routine_id)).toHaveLength(1);
  });

  it("leaves a run queued when the backend is asleep and refuses to dispatch", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const result = await runScheduler(env, BASE + 2000, {
      fetchImpl: stubFetch({ healthUp: false }),
    });
    expect(result.backendReady).toBe(false);
    expect(result.queued).toBe(1);
    expect(result.completed).toBe(0);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("queued");
  });

  it("delays a rate-limited run rather than failing or hot-retrying it", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const now = BASE + 2000;
    const result = await runScheduler(env, now, {
      fetchImpl: stubFetch({
        healthUp: true,
        execute: () => json({ ok: false, error_code: "rate_limited" }, 429),
      }),
    });
    expect(result.delayed).toBe(1);
    expect(result.failed).toBe(0);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("delayed");
    expect(runs[0].available_at).toBe(now + 60_000); // ROUTINE_RETRY_BASE_MS
  });

  it("fails a run the backend rejects without retry", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const result = await runScheduler(env, BASE + 2000, {
      fetchImpl: stubFetch({
        healthUp: true,
        execute: () => json({ ok: false, error: "bad request", retryable: false }, 400),
      }),
    });
    expect(result.failed).toBe(1);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("failed");
    expect(runs[0].error_code).toBe("bad request");
  });

  it("runs passive summaries when the backend is up but no routine is due", async () => {
    let summaryCalls = 0;
    const result = await runScheduler(env, BASE, {
      fetchImpl: stubFetch({
        healthUp: true,
        summaries: () => {
          summaryCalls += 1;
          return json({ ok: true });
        },
      }),
    });
    expect(result.dispatched).toBe(0);
    expect(result.backendReady).toBe(true);
    expect(result.summaries).toBe("ran");
    expect(summaryCalls).toBe(1);
  });
});
