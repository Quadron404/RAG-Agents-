/**
 * Codespace lifecycle: wake the backend when a routine is due.
 *
 * A routine has to be able to fire "while the Codespace is stopped", which means
 * the scheduler cannot assume the backend is already there.  This module owns
 * the two questions that need answering before any ordinary task is dispatched:
 *
 *   1. Is the backend actually ready?  ``probeBackend`` asks the backend's own
 *      ``/health``.  A started Codespace is not the same as a listening backend,
 *      and dispatching into a half-booted one would just lose the run.
 *   2. If it is not ready and we are allowed to wake it, start the Codespace via
 *      the GitHub API and wait for ``/health`` to answer.
 *
 * Everything is injectable (``fetchImpl``, ``sleep``) so the calls can be tested
 * with a stubbed GitHub and a stubbed backend, without a Codespace or a PAT.
 * When the PAT or the Codespace name is missing the function does not pretend:
 * it reports ``ready: false`` and names the reason, and the caller leaves the
 * run durably queued.
 */
import type { Env } from "./env";

export interface LifecycleDeps {
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
}

export interface ProbeResult {
  up: boolean;
  status: number;
  detail: string;
}

export interface ReadyResult {
  ready: boolean;
  woke: boolean;
  detail: string;
}

function intFrom(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function githubBase(env: Env): string {
  return (env.GITHUB_API_URL || "https://api.github.com").replace(/\/+$/, "");
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** True when the scheduler is allowed (and able) to start a stopped Codespace. */
export function lifecycleConfigured(env: Env): boolean {
  return Boolean(env.GITHUB_TOKEN && env.CODESPACE_NAME);
}

/**
 * Ask the backend whether it is listening.
 *
 * A short timeout: this is called on every tick, and a hung backend must not
 * hold the tick open.  Any non-2xx answer, a network error or a timeout all mean
 * "not ready".
 */
export async function probeBackend(env: Env, deps: LifecycleDeps = {}): Promise<ProbeResult> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const origin = (env.BACKEND_ORIGIN || "").replace(/\/+$/, "");
  if (!origin) return { up: false, status: 0, detail: "backend origin is not configured" };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetchImpl(`${origin}/health`, {
      method: "GET",
      headers: { accept: "application/json" },
      signal: controller.signal,
    });
    const body = await response.text();
    let ok = false;
    try {
      ok = Boolean((JSON.parse(body) as { ok?: unknown }).ok);
    } catch {
      ok = response.ok;
    }
    return { up: response.ok && ok, status: response.status, detail: `http ${response.status}` };
  } catch (error) {
    return { up: false, status: 0, detail: `unreachable: ${(error as Error).message}` };
  } finally {
    clearTimeout(timer);
  }
}

/** Ask GitHub to start the Codespace.  Idempotent: a running one stays running. */
export async function startCodespace(
  env: Env,
  deps: LifecycleDeps = {},
): Promise<{ ok: boolean; status: number; detail: string }> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  if (!env.GITHUB_TOKEN || !env.CODESPACE_NAME) {
    return { ok: false, status: 0, detail: "codespace lifecycle is not configured" };
  }
  try {
    const response = await fetchImpl(
      `${githubBase(env)}/user/codespaces/${encodeURIComponent(env.CODESPACE_NAME)}/start`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${env.GITHUB_TOKEN}`,
          accept: "application/vnd.github+json",
          "x-github-api-version": "2022-11-28",
          "user-agent": "rag-agents-worker",
        },
      },
    );
    const body = await response.text();
    let state = "";
    try {
      state = String((JSON.parse(body) as { state?: unknown }).state ?? "");
    } catch {
      // ignore a non-JSON body
    }
    // 200/201/202 all mean "started or starting".
    const ok = response.status >= 200 && response.status < 300;
    return { ok, status: response.status, detail: state ? `state=${state}` : `http ${response.status}` };
  } catch (error) {
    return { ok: false, status: 0, detail: `github unreachable: ${(error as Error).message}` };
  }
}

/**
 * Ensure the backend is up before an ordinary task is dispatched.
 *
 * ``wake: false`` is used on the "summaries only" path: a quiet conversation is
 * not a reason to burn Codespace minutes, so an asleep backend is simply left
 * asleep and the summaries wait for the next tick that does have real work.
 */
export async function ensureBackendReady(
  env: Env,
  options: { wake: boolean; timeoutMs?: number; intervalMs?: number } = { wake: true },
  deps: LifecycleDeps = {},
): Promise<ReadyResult> {
  const sleep = deps.sleep ?? defaultSleep;
  const timeoutMs = options.timeoutMs ?? intFrom(env.CODESPACE_READY_TIMEOUT_MS, 60000);
  const intervalMs = options.intervalMs ?? intFrom(env.CODESPACE_POLL_INTERVAL_MS, 5000);

  const first = await probeBackend(env, deps);
  if (first.up) return { ready: true, woke: false, detail: "backend already up" };

  if (!options.wake) {
    return { ready: false, woke: false, detail: `backend asleep (${first.detail})` };
  }
  if (!lifecycleConfigured(env)) {
    return {
      ready: false,
      woke: false,
      detail: "codespace lifecycle is not configured (GITHUB_TOKEN / CODESPACE_NAME)",
    };
  }

  const started = await startCodespace(env, deps);
  if (!started.ok) {
    return { ready: false, woke: false, detail: `could not start codespace: ${started.detail}` };
  }

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(intervalMs);
    const probe = await probeBackend(env, deps);
    if (probe.up) return { ready: true, woke: true, detail: `backend ready (${probe.detail})` };
  }
  return { ready: false, woke: true, detail: "codespace started but the backend did not become ready in time" };
}
