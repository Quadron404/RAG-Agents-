import { SELF, env, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
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

function stubBackend(path: string, method = "GET"): void {
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
    const response = await SELF.fetch(`${BASE}/threads`);
    expect(response.status).toBe(401);
  });

  it("forwards an authenticated request with the gateway token and identity", async () => {
    stubBackend("/threads");
    const cookie = await authenticatedCookie();
    const response = await SELF.fetch(`${BASE}/threads`, { headers: { cookie } });
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
    const response = await SELF.fetch(`${BASE}/threads`, {
      method: "POST",
      headers: { cookie, "content-type": "application/json" },
      body: JSON.stringify({ title: "x" }),
    });
    expect(response.status).toBe(403);
    expect(captured.calls).toBe(0);
  });

  it("forwards a state-changing proxy call that carries the CSRF token", async () => {
    stubBackend("/threads", "POST");
    const { cookie, csrf } = await authenticatedSession();
    const response = await SELF.fetch(`${BASE}/threads`, {
      method: "POST",
      headers: { cookie, "content-type": "application/json", "x-rag-csrf": csrf },
      body: JSON.stringify({ title: "x" }),
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
