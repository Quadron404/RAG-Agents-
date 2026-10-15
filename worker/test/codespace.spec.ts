import { describe, expect, it } from "vitest";
import { ensureBackendReady, probeBackend, startCodespace } from "../src/codespace";
import type { Env } from "../src/env";

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function fakeEnv(overrides: Partial<Env> = {}): Env {
  return {
    BACKEND_ORIGIN: "https://backend.test",
    ...overrides,
  } as unknown as Env;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("probeBackend", () => {
  it("is up only when /health says ok", async () => {
    const up: FetchLike = async () => json({ ok: true });
    expect((await probeBackend(fakeEnv(), { fetchImpl: up })).up).toBe(true);

    const degraded: FetchLike = async () => json({ ok: false }, 200);
    expect((await probeBackend(fakeEnv(), { fetchImpl: degraded })).up).toBe(false);

    const clinic: FetchLike = async () => json({}, 500);
    expect((await probeBackend(fakeEnv(), { fetchImpl: clinic })).up).toBe(false);
  });
});

describe("startCodespace", () => {
  it("refuses to act when the lifecycle is not configured", async () => {
    const result = await startCodespace(fakeEnv(), {});
    expect(result.ok).toBe(false);
    expect(result.detail).toContain("not configured");
  });

  it("posts to the GitHub codespaces start endpoint", async () => {
    const seen: string[] = [];
    const fetchImpl: FetchLike = async (input) => {
      seen.push(String(input));
      return json({ state: "Starting" }, 202);
    };
    const result = await startCodespace(
      fakeEnv({ GITHUB_TOKEN: "pat", CODESPACE_NAME: "my-box" }),
      { fetchImpl },
    );
    expect(result.ok).toBe(true);
    expect(seen[0]).toBe("https://api.github.com/user/codespaces/my-box/start");
  });
});

describe("ensureBackendReady", () => {
  it("reports ready without waking when the backend is already up", async () => {
    const fetchImpl: FetchLike = async () => json({ ok: true });
    const result = await ensureBackendReady(fakeEnv(), { wake: true }, { fetchImpl });
    expect(result).toEqual({ ready: true, woke: false, detail: "backend already up" });
  });

  it("never wakes for a summaries-only tick", async () => {
    const fetchImpl: FetchLike = async () => {
      throw new Error("down");
    };
    const result = await ensureBackendReady(fakeEnv(), { wake: false }, { fetchImpl });
    expect(result.ready).toBe(false);
    expect(result.woke).toBe(false);
    expect(result.detail).toContain("asleep");
  });

  it("does not pretend when lifecycle is unconfigured", async () => {
    const fetchImpl: FetchLike = async () => {
      throw new Error("down");
    };
    const result = await ensureBackendReady(fakeEnv(), { wake: true }, { fetchImpl });
    expect(result.ready).toBe(false);
    expect(result.detail).toContain("not configured");
  });

  it("starts the Codespace and waits for /health before declaring ready", async () => {
    let healthCalls = 0;
    const fetchImpl: FetchLike = async (input) => {
      const url = String(input);
      if (url.endsWith("/health")) {
        healthCalls += 1;
        // Down at first, then up once the Codespace has been started.
        return healthCalls >= 2 ? json({ ok: true }) : json({}, 503);
      }
      if (url.endsWith("/start")) return json({ state: "Starting" }, 202);
      throw new Error(`unexpected ${url}`);
    };
    const result = await ensureBackendReady(
      fakeEnv({ GITHUB_TOKEN: "pat", CODESPACE_NAME: "my-box" }),
      { wake: true, timeoutMs: 60_000, intervalMs: 1 },
      { fetchImpl, sleep: async () => {} },
    );
    expect(result.ready).toBe(true);
    expect(result.woke).toBe(true);
  });
});
