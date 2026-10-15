import { SELF, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const BASE = "https://example.com";
const TOKEN = "test-internal-token";

// The scheduler route probes the backend and pings summaries; stub those so the
// test neither depends on nor leaks a real outbound request.
beforeAll(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
});

afterAll(() => {
  fetchMock.assertNoPendingInterceptors();
});

function stubBackend(path: string, method: string): void {
  fetchMock
    .get("https://backend.test")
    .intercept({ path, method })
    .reply(() => ({ statusCode: 200, data: JSON.stringify({ ok: true }) }));
}

function authed(body?: unknown, method = "GET"): RequestInit {
  return {
    method,
    headers: { "x-rag-internal-token": TOKEN, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

async function call(path: string, init: RequestInit): Promise<{ status: number; body: any }> {
  const response = await SELF.fetch(`${BASE}${path}`, init);
  return { status: response.status, body: await response.json() };
}

async function createOne(owner = "int-sub"): Promise<string> {
  const { body } = await call(
    "/internal/routines",
    authed(
      {
        owner_sub: owner,
        owner_id: "user-1",
        agent_id: "agent-1",
        name: "Inbox digest",
        task_prompt: "Summarise the inbox",
        schedule_type: "daily",
        schedule: { time: "09:00" },
        timezone: "UTC",
      },
      "POST",
    ),
  );
  return body.routine.routine_id;
}

describe("internal routines API", () => {
  it("rejects a request without the internal token", async () => {
    const response = await SELF.fetch(`${BASE}/internal/routines?owner_sub=x`);
    expect(response.status).toBe(401);
  });

  it("rejects a request with the wrong token", async () => {
    const response = await SELF.fetch(`${BASE}/internal/routines?owner_sub=x`, {
      headers: { "x-rag-internal-token": "nope" },
    });
    expect(response.status).toBe(401);
  });

  it("creates, reads, edits, pauses, resumes and deletes a routine", async () => {
    const routineId = await createOne();

    const listed = await call("/internal/routines?owner_sub=int-sub", authed());
    expect(listed.body.routines.map((r: { routine_id: string }) => r.routine_id)).toContain(routineId);

    const fetched = await call(`/internal/routines/${routineId}`, authed());
    expect(fetched.body.routine.name).toBe("Inbox digest");

    const edited = await call(`/internal/routines/${routineId}`, authed({ name: "Renamed" }, "PATCH"));
    expect(edited.body.routine.name).toBe("Renamed");

    const paused = await call(`/internal/routines/${routineId}/pause`, authed({}, "POST"));
    expect(paused.body.routine.enabled).toBe(0);

    const resumed = await call(`/internal/routines/${routineId}/resume`, authed({}, "POST"));
    expect(resumed.body.routine.enabled).toBe(1);

    const runs = await call(`/internal/routines/${routineId}/runs`, authed());
    expect(runs.body.runs).toEqual([]);

    const removed = await call(`/internal/routines/${routineId}`, authed(undefined, "DELETE"));
    expect(removed.body.ok).toBe(true);

    const gone = await call(`/internal/routines/${routineId}`, authed());
    expect(gone.status).toBe(404);
  });

  it("will not reveal another owner's routine", async () => {
    const routineId = await createOne("owner-a");
    const response = await SELF.fetch(`${BASE}/internal/routines/${routineId}?owner_sub=owner-b`, {
      headers: { "x-rag-internal-token": TOKEN },
    });
    expect(response.status).toBe(404);
  });

  it("runs a scheduler tick on demand", async () => {
    stubBackend("/health", "GET");
    stubBackend("/internal/summaries/run", "POST");
    const { status, body } = await call("/internal/scheduler/run", authed({}, "POST"));
    expect(status).toBe(200);
    expect(body.ok).toBe(true);
    expect(body).toHaveProperty("materialized");
  });
});
