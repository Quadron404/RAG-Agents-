import { env, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createRoutine, getRoutine, listRuns } from "../src/routines";
import { runScheduler } from "../src/scheduler";

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

/** Stub the Groq chat endpoint so the scheduler's edge execution gets a real answer. */
function stubGroq(answer: string, status = 200, bodyOverride?: unknown) {
  const body =
    status === 200
      ? JSON.stringify({
          choices: [{ message: { role: "assistant", content: answer }, finish_reason: "stop" }],
        })
      : JSON.stringify(bodyOverride ?? { error: { message: "provider error" } });
  fetchMock
    .get("https://api.groq.com")
    .intercept({ path: "/openai/v1/chat/completions", method: "POST" })
    .reply(status, body, { headers: { "content-type": "application/json" } });
}

describe("runScheduler", () => {
  beforeAll(() => {
    fetchMock.activate();
    fetchMock.disableNetConnect();
  });

  afterAll(() => {
    fetchMock.assertNoPendingInterceptors();
  });

  it("materialises and completes a due one-time routine, recording the real result", async () => {
    const routine = await onceRoutine(BASE + 1000);
    stubGroq("sent the digest");

    const result = await runScheduler(env, BASE + 2000);

    expect(result.materialized).toBe(1);
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
    stubGroq("first result");
    await runScheduler(env, BASE + 2000);
    const second = await runScheduler(env, BASE + 2000);
    expect(second.materialized).toBe(0);
    expect(second.executed).toBe(0);
    expect(await listRuns(env, routine.routine_id)).toHaveLength(1);
  });

  it("delays a rate-limited run rather than failing or hot-retrying it", async () => {
    const routine = await onceRoutine(BASE + 1000);
    const now = BASE + 2000;
    fetchMock
      .get("https://api.groq.com")
      .intercept({ path: "/openai/v1/chat/completions", method: "POST" })
      .reply(429, JSON.stringify({ error: { message: "rate limited" } }), {
        headers: { "content-type": "application/json", "retry-after": "60" },
      });

    const result = await runScheduler(env, now);
    expect(result.delayed).toBe(1);
    expect(result.failed).toBe(0);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("delayed");
    expect(runs[0].available_at).toBe(now + 60_000); // ROUTINE_RETRY_BASE_MS
  });

  it("fails a run the provider rejects without retry", async () => {
    const routine = await onceRoutine(BASE + 1000);
    fetchMock
      .get("https://api.groq.com")
      .intercept({ path: "/openai/v1/chat/completions", method: "POST" })
      .reply(400, JSON.stringify({ error: { message: "bad request" } }), {
        headers: { "content-type": "application/json" },
      });

    const result = await runScheduler(env, BASE + 2000);
    expect(result.failed).toBe(1);
    const runs = await listRuns(env, routine.routine_id);
    expect(runs[0].status).toBe("failed");
    expect(runs[0].error_code).toContain("bad request");
  });

  it("keeps summaries on the Worker and skips when nothing is due", async () => {
    const result = await runScheduler(env, BASE);
    expect(result.materialized).toBe(0);
    expect(result.executed).toBe(0);
    expect(result.summaries).toBe("skipped");
  });
});
