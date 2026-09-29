import { apiFetch, wsUrl } from "../core";

/* ============================================================================
   Where the live screen lives.

   Two routes, one implementation of the viewer:

   tunnel  (deployed)
     A Cloudflare Quick Tunnel publishes the RAG Agents app on a random
     trycloudflare.com origin.  The Computer view opens a WebSocket to
     /websockify on that same origin; the app proxies the last hop to websockify
     on the machine's loopback.

     The tunnel URL is not a secret, so it is not what protects the screen: the
     session cookie is.  Because /websockify is same-origin with the app, the
     cookie the browser already presented to load the page rides along with the
     upgrade automatically -- no second login, no token in the URL.

     The hostname changes every time the tunnel restarts, so it is never
     hardcoded.  The server reads the live one from the tunnel script and
     /screen/config hands it over, which is what lets a restarted tunnel be
     picked up on the next load.

   bridge  (local development, or no tunnel running)
     The backend relays raw RFB over its own WebSocket at /ws/screen.  Same
     protocol, same noVNC client, one extra hop, and no Cloudflare involved.

   Both require the same session, so neither is a way around the lock screen.
   ========================================================================== */

export type ScreenMode = "tunnel" | "bridge";

export interface ScreenConfig {
  mode: ScreenMode;
  /** The public, session-gated endpoint. Empty in bridge mode. */
  wsUrl: string;
  /** Backend path used only in bridge mode. */
  bridgePath: string;
  /** noVNC negotiates the binary subprotocol; websockify serves it. */
  wsProtocols: string[];
  /** The tunnel's current origin, for display. */
  publicOrigin?: string;
  /** How long ago that URL was published; large means the tunnel has moved on. */
  publicUrlAgeSeconds?: number;
  authRequired?: boolean;
}

const FALLBACK: ScreenConfig = {
  mode: "bridge",
  wsUrl: "",
  bridgePath: "/ws/screen",
  wsProtocols: ["binary"],
};

let cached: ScreenConfig | null = null;
let inflight: Promise<ScreenConfig> | null = null;
let fetchedAt = 0;

/**
 * How long one answer is trusted.
 *
 * A Quick Tunnel is given a new random hostname every time it restarts, so a
 * config cached for the life of the page would keep pointing noVNC at an
 * address that no longer resolves.  Re-reading on a timer means a tab left open
 * across a tunnel restart reconnects to the new origin by itself.
 */
const TTL_MS = 30_000;

/**
 * Fetch how the viewer should connect, falling back to the bridge.
 *
 * Cached briefly (not forever) to absorb the bursts of mounts that happen when
 * several views come up together, while still noticing a new tunnel URL.
 */
export function loadScreenConfig(force = false): Promise<ScreenConfig> {
  const fresh = Date.now() - fetchedAt < TTL_MS;
  if (!force && cached && fresh) return Promise.resolve(cached);
  // De-duplicate the concurrent calls that happen when two views mount at once.
  if (!force && inflight) return inflight;
  const p = apiFetch("/screen/config")
    .then((r) => (r.ok ? (r.json() as Promise<ScreenConfig>) : FALLBACK))
    .then((cfg) => {
      cached = {
        mode: cfg?.mode === "tunnel" && cfg?.wsUrl ? "tunnel" : "bridge",
        wsUrl: cfg?.wsUrl || "",
        bridgePath: cfg?.bridgePath || FALLBACK.bridgePath,
        wsProtocols: cfg?.wsProtocols?.length ? cfg.wsProtocols : FALLBACK.wsProtocols,
        publicOrigin: cfg?.publicOrigin,
        publicUrlAgeSeconds: cfg?.publicUrlAgeSeconds,
        authRequired: cfg?.authRequired,
      };
      return cached;
    })
    .catch(() => FALLBACK)
    .then((cfg) => {
      cached = cfg;
      fetchedAt = Date.now();
      inflight = null;
      return cfg;
    });
  if (!force) inflight = p;
  return p;
}

/** The WebSocket URL noVNC should open, given the resolved config. */
export function screenSocketUrl(cfg: ScreenConfig): string {
  return cfg.mode === "tunnel" ? cfg.wsUrl : wsUrl(cfg.bridgePath);
}

/* --- live tunnel watch ----------------------------------------------------- */

/**
 * Watch for the tunnel being handed a new hostname, and say so.
 *
 * A Quick Tunnel gets a random origin on every start, so a long-lived tab can
 * end up pointing noVNC at a hostname Cloudflare has already forgotten.  Two
 * things have to notice that, and only one of them used to:
 *
 *   - the connection itself, which fails and retries with backoff.  That does
 *     recover, but only by accident: the retry can fire while the 30s cache is
 *     still warm, hand the same dead origin back, and take several rounds of
 *     growing backoff to eventually read a fresh config.
 *   - this poll, which compares the URL it is pointed at against the one the
 *     server reports and fires the moment they differ.
 *
 * So a restarted tunnel is picked up in one poll interval instead of whenever
 * the backoff happens to exceed the cache TTL.  Returns an unsubscribe.
 */
const WATCH_INTERVAL_MS = 15_000;

export function watchScreenConfig(
  onChange: (cfg: ScreenConfig) => void,
  intervalMs: number = WATCH_INTERVAL_MS
): () => void {
  let stopped = false;
  let lastSocketUrl = "";

  const tick = async () => {
    if (stopped) return;
    let cfg: ScreenConfig;
    try {
      // force: the cache is exactly what would hide a changed URL.
      cfg = await loadScreenConfig(true);
    } catch {
      return;
    }
    if (stopped) return;
    const next = screenSocketUrl(cfg);
    if (!next || next === lastSocketUrl) return;
    const first = lastSocketUrl === "";
    lastSocketUrl = next;
    if (!first) onChange(cfg);
  };

  void tick();
  const id = setInterval(tick, intervalMs);
  return () => {
    stopped = true;
    clearInterval(id);
  };
}

/* --- status and controls -------------------------------------------------- */

export interface ScreenStatus {
  ok: boolean;
  host: string;
  port: number;
  listening: boolean;
  note?: string;
  display?: {
    display?: string;
    size?: string;
    depth?: number;
    x?: boolean;
    vnc?: boolean;
    vnc_clients?: number;
    /** True when the RFB->WebSocket hop is up, i.e. the screen is reachable. */
    websockify?: boolean;
    websockify_port?: number;
    chromium?: boolean;
    restarts?: Record<string, number>;
    error?: string;
  };
}

export async function fetchScreenStatus(): Promise<ScreenStatus> {
  const res = await apiFetch("/screen/status");
  if (!res.ok) throw new Error(`screen status ${res.status}`);
  return (await res.json()) as ScreenStatus;
}

/** Ask the remote machine to bring its display stack up. */
export async function ensureDisplay(): Promise<void> {
  await apiFetch("/screen/ensure", { method: "POST" });
}

/** Restart the remote browser; the screen then follows the real state. */
export async function restartRemoteBrowser(): Promise<void> {
  await apiFetch("/screen/browser/restart", { method: "POST" });
}

/* --- computer control ------------------------------------------------------ */

/**
 * The status the indicator renders.  The backend sends only this and a message:
 * no coordinates, no model output, and never the API key, because this is the
 * half of computer control that lives in the browser.
 */
export type ComputerStatus = "idle" | "observing" | "controlling" | "done" | "error";

export interface ComputerRun {
  task_id: string;
  status: ComputerStatus;
  step: number;
  message: string;
  url: string;
  steps: number;
  running: boolean;
  done: boolean;
}

/** Hand a task to the model.  Returns as soon as the run is queued. */
export async function startComputerTask(task: string, threadId = ""): Promise<ComputerRun> {
  const res = await apiFetch("/ai/computer/start", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ task, thread_id: threadId }),
  });
  const data = (await res.json()) as ComputerRun & { error?: string };
  if (data.error) throw new Error(data.error);
  return data;
}

export async function fetchComputerTask(taskId: string): Promise<ComputerRun> {
  const res = await apiFetch(`/ai/computer/${taskId}`);
  const data = (await res.json()) as ComputerRun & { error?: string };
  if (data.error) throw new Error(data.error);
  return data;
}

export async function stopComputerTask(taskId: string): Promise<void> {
  await apiFetch(`/ai/computer/${taskId}/stop`, { method: "POST" });
}
