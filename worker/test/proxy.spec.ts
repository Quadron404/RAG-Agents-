import { SELF, env, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { buildUpstreamRequest } from "../src/proxy";
import { createSession, upsertUser } from "../src/store";
import { BASE } from "./helpers";

interface Captured {
  headers: Record<string, unknown>;
  calls: number;
}

let captured: Captured;

beforeAll(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
});

beforeEach(() => {
  captured = { headers: {}, calls: 0 };
});

afterAll(() => {
  fetchMock.assertNoPendingInterceptors();
});

function stubBackend(path: string | RegExp, method = "GET"): void {
  fetchMock
    .get("https://backend.test")
    .intercept({ path, method })
    .reply((options) => {
      captured.calls += 1;
      captured.headers = (options as { headers?: Record<string, unknown> }).headers ?? {};
      return { statusCode: 200, data: JSON.stringify({ ok: true }) };
    });
}

async function authenticatedCookie(): Promise<string> {
  return (await authenticatedSession()).cookie;
}

async function authenticatedSession(): Promise<{ cookie: string; csrf: string }> {
  const user = await upsertUser(env, {
    sub: "proxy-sub",
    email: "proxy@example.com",
    email_verified: true,
    name: "Proxy",
    picture: "https://img.example/p.png",
  });
  const session = await createSession(env, user.id);
  return { cookie: `rag_session=${session.token}`, csrf: session.csrf };
}

describe("reverse proxy", () => {
  it("refuses an API request with no session", async () => {
    const response = await SELF.fetch(`${BASE}/ai`);
    expect(response.status).toBe(401);
  });

  it("forwards an authenticated request with the gateway token and identity", async () => {
    stubBackend("/ai");
    const cookie = await authenticatedCookie();
    const response = await SELF.fetch(`${BASE}/ai`, { headers: { cookie } });
    expect(response.status).toBe(200);
    expect(captured.calls).toBe(1);
    expect(captured.headers["x-rag-internal-token"]).toBe("test-internal-token");
    expect(captured.headers["x-rag-user-sub"]).toBe("proxy-sub");
    expect(captured.headers["x-rag-user-email"]).toBe("proxy@example.com");
    expect(captured.headers["x-rag-user-email-verified"]).toBe("1");
    expect(captured.headers["x-rag-user-picture"]).toBe("https://img.example/p.png");
  });

  it("strips a client-supplied gateway token and identity", async () => {
    stubBackend("/sysinfo");
    const cookie = await authenticatedCookie();
    await SELF.fetch(`${BASE}/sysinfo`, {
      headers: {
        cookie,
        "x-rag-internal-token": "attacker-token",
        "x-rag-user-id": "attacker",
        "x-rag-user-sub": "attacker",
        "x-rag-user-email": "attacker@example.com",
        "x-rag-user-email-verified": "1",
      },
    });
    expect(captured.headers["x-rag-internal-token"]).toBe("test-internal-token");
    expect(captured.headers["x-rag-user-sub"]).toBe("proxy-sub");
    expect(captured.headers["x-rag-user-email"]).toBe("proxy@example.com");
  });

  it("refuses a state-changing proxy call without a CSRF token", async () => {
    const cookie = await authenticatedCookie();
    const response = await SELF.fetch(`${BASE}/ai`, {
      method: "POST",
      headers: { cookie, "content-type": "application/json" },
      body: JSON.stringify({ prompt: "x" }),
    });
    expect(response.status).toBe(403);
    expect(captured.calls).toBe(0);
  });

  it("forwards a state-changing proxy call that carries the CSRF token", async () => {
    stubBackend("/ai", "POST");
    const { cookie, csrf } = await authenticatedSession();
    const response = await SELF.fetch(`${BASE}/ai`, {
      method: "POST",
      headers: { cookie, "content-type": "application/json", "x-rag-csrf": csrf },
      body: JSON.stringify({ prompt: "x" }),
    });
    expect(response.status).toBe(200);
    expect(captured.calls).toBe(1);
  });

  it("proxies /health without a session", async () => {
    stubBackend("/health");
    const response = await SELF.fetch(`${BASE}/health`);
    expect(response.status).toBe(200);
    expect(captured.calls).toBe(1);
  });
});

// The routines surface the UI drives (list, runs, pause, resume, delete) is
// now handled natively at the Worker edge: it reads from the same D1 the
// scheduler writes to, so scheduled tasks show even when the Codespace
// backend is asleep.  These checks are the routing regression, plus proof
// that the session, the CSRF token and the owner identity are enforced
// exactly as other API paths are.
describe("routine edge routing", () => {
  async function createRoutineViaInternal(ownerSub: string, name = "Inbox digest"): Promise<string> {
    const response = await SELF.fetch(`${BASE}/internal/routines`, {
      method: "POST",
      headers: {
        "x-rag-internal-token": "test-internal-token",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        owner_sub: ownerSub,
        owner_id: "user-1",
        agent_id: "agent-1",
        name,
        task_prompt: "Summarise the inbox",
        schedule_type: "daily",
        schedule: { time: "09:00" },
        timezone: "UTC",
      }),
    });
    if (response.status !== 200) {
      const text = await response.text();
      throw new Error(`internal create failed: ${response.status} ${text}`);
    }
    const body = await response.json();
    return body.routine.routine_id;
  }

  it("treats /routines as an API path instead of a static asset", async () => {
    const response = await SELF.fetch(`${BASE}/routines`);
    expect(response.status).toBe(401);
  });

  it("lists routines for the session owner without proxying to the backend", async () => {
    const routineId = await createRoutineViaInternal("proxy-sub");
    const cookie = await authenticatedCookie();
    const response = await SELF.fetch(`${BASE}/routines`, { headers: { cookie } });
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.ok).toBe(true);
    expect(body.available).toBe(true);
    expect(body.routines.map((r: { routine_id: string }) => r.routine_id)).toContain(routineId);
  });

  it("keeps nested routine runs on the edge path", async () => {
    const routineId = await createRoutineViaInternal("proxy-sub");
    const cookie = await authenticatedCookie();
    const response = await SELF.fetch(`${BASE}/routines/${routineId}/runs?limit=5`, {
      headers: { cookie },
    });
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.ok).toBe(true);
    expect(body.available).toBe(true);
    expect(Array.isArray(body.runs)).toBe(true);
  });

  it("refuses a routine mutation without a CSRF token", async () => {
    const routineId = await createRoutineViaInternal("proxy-sub");
    const cookie = await authenticatedCookie();
    const response = await SELF.fetch(`${BASE}/routines/${routineId}/pause`, {
      method: "POST",
      headers: { cookie, "content-type": "application/json" },
    });
    expect(response.status).toBe(403);
  });

  it("forwards a routine mutation carrying the CSRF token", async () => {
    const routineId = await createRoutineViaInternal("proxy-sub");
    const { cookie, csrf } = await authenticatedSession();
    const response = await SELF.fetch(`${BASE}/routines/${routineId}/pause`, {
      method: "POST",
      headers: { cookie, "content-type": "application/json", "x-rag-csrf": csrf },
    });
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.ok).toBe(true);
    expect(body.available).toBe(true);
    expect(body.routine.enabled).toBe(0);
  });

  it("forwards a routine deletion carrying the CSRF token", async () => {
    const routineId = await createRoutineViaInternal("proxy-sub");
    const { cookie, csrf } = await authenticatedSession();
    const response = await SELF.fetch(`${BASE}/routines/${routineId}`, {
      method: "DELETE",
      headers: { cookie, "x-rag-csrf": csrf },
    });
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.ok).toBe(true);
  });

  it("strips a client-supplied owner identity on a routine read", async () => {
    await createRoutineViaInternal("proxy-sub");
    const cookie = await authenticatedCookie();
    await SELF.fetch(`${BASE}/routines`, {
      headers: {
        cookie,
        "x-rag-internal-token": "attacker-token",
        "x-rag-user-sub": "victim",
        "x-rag-user-email": "victim@example.com",
      },
    });
    // The edge resolves the owner from the session cookie, not from headers.
    const response = await SELF.fetch(`${BASE}/routines`, { headers: { cookie } });
    const body = await response.json();
    expect(body.ok).toBe(true);
    for (const r of body.routines) {
      expect(r.owner_sub).toBe("proxy-sub");
    }
  });
});

// The Computer screen opens a WebSocket to /websockify.  It has to ride the
// same authenticated proxy as the REST API: /screen/config used to advertise the
// raw Quick Tunnel, which bypasses the Worker, so the backend saw no internal
// token and closed the handshake with a 403.  Forwarding the WebSocket means
// rebuilding the upstream request without dropping the upgrade handshake
// headers -- a WebSocket whose Upgrade/Connection/Sec-WebSocket-* headers are
// lost is just a plain GET to the backend, which answers a 400 -- while still
// injecting the gateway token and stripping the browser's cookie.
describe("websocket upgrade proxy", () => {
  it("forwards the upgrade handshake with the gateway token and identity", async () => {
    // A live WebSocket upgrade does not surface to `fetchMock` -- workerd dials
    // the real backend and refuses, so the interceptor never fires.  The unit
    // under test is the request the proxy builds for the backend, which is where
    // the handshake headers are either kept or lost, and it is driven directly.
    const user = await upsertUser(env, {
      sub: "proxy-sub",
      email: "proxy@example.com",
      email_verified: true,
      name: "Proxy",
      picture: "https://img.example/p.png",
    });
    const session = await createSession(env, user.id);
    const url = new URL(`${BASE}/websockify`);
    const incoming = new Request(url.toString(), {
      headers: {
        cookie: `rag_session=${session.token}`,
        Upgrade: "websocket",
        Connection: "Upgrade",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Protocol": "binary",
      },
    });

    const upstream = buildUpstreamRequest(incoming, env, url, {
      user,
      session: {
        session_hash: session.sessionHash,
        user_id: user.id,
        csrf: session.csrf,
        created_at: Date.now(),
        expires_at: session.expiresAt,
        revoked: 0,
        rotated_from: "",
      },
    });

    expect(upstream.url).toBe("https://backend.test/websockify");
    const headers = upstream.headers;
    expect(String(headers.get("upgrade")).toLowerCase()).toBe("websocket");
    expect(String(headers.get("connection")).toLowerCase()).toBe("upgrade");
    expect(headers.get("sec-websocket-key")).toBe("dGhlIHNhbXBsZSBub25jZQ==");
    expect(headers.get("sec-websocket-version")).toBe("13");
    expect(headers.get("sec-websocket-protocol")).toBe("binary");
    expect(headers.get("x-rag-internal-token")).toBe("test-internal-token");
    expect(headers.get("x-rag-user-sub")).toBe("proxy-sub");
    expect(headers.get("x-rag-user-email")).toBe("proxy@example.com");
    // The browser cookie is not forwarded: the edge asserts identity instead, so
    // the backend never has to trust a same-site cookie it did not issue.
    expect(headers.get("cookie")).toBeNull();
  });

  it("refuses an unauthenticated websocket upgrade before it reaches the backend", async () => {
    const response = await SELF.fetch(`${BASE}/websockify`, {
      headers: { Upgrade: "websocket", Connection: "Upgrade" },
    });
    expect(response.status).toBe(401);
    expect(captured.calls).toBe(0);
  });
});
