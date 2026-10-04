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
export type ComputerStatus = "idle" | "observing" | "controlling" | "done" | "stopped" | "error";

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
export async function startComputerTask(task: string, threadId = "", provider = ""): Promise<ComputerRun> {
  const res = await apiFetch("/ai/computer/start", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ task, thread_id: threadId, provider }),
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

/**
 * One message as it went out, without the text.  The roles and sizes are the
 * part that is worth showing; the full text of every history message would
 * bury the one message that matters, the prompt.
 */
export interface ComputerMessageMeta {
  role: string;
  chars: number;
  images: number;
  image_bytes: number;
  content_preview: string;
}

/** What the request looked like once serialised, from the body that was sent. */
export interface ComputerWire {
  path: string;
  model: string | null;
  /**
   * How many messages actually reached the API.
   *
   * Optional because it is absent on a turn where no request was serialised --
   * a provider that was never configured, a request that raised before the body
   * was built.  "Not reported" is a third state and is rendered as such; reading
   * a missing count as `0`, or as a failed comparison against the runner's own
   * count, is what turned an absent field into a red "serialisation failed".
   */
  messages_count?: number;
  roles: string[];
  text_parts: number;
  image_count: number;
  image_present: boolean;
  image_mime: string;
  image_payload_type: string;
  content_part_types: string[];
  first_image_message_index: number | null;
  stream: boolean;
  response_format: string | null;
  tools_count: number;
  /** How many messages the runner built, for cross-checking against the wire. */
  source_message_count?: number;
  /**
   * Whether the body was serialisable and carried every message the runner
   * built -- decided by the serialiser, which is the only place the answer
   * exists.  Absent means the turn recorded no serialised request at all, which
   * is neither a success nor a failure.
   */
  serialized_ok?: boolean;
  /** Why the body could not be serialised, when it could not be. */
  serialization_error?: string;
}

export interface ComputerImageMeta {
  width: number;
  height: number;
  mime: string;
  bytes_b64: number;
  sha256_16: string;
}

/** What the machine did with the command, and where the pointer ended up. */
export interface ComputerExecution {
  accepted: boolean;
  executed: boolean;
  /** "executed" | "done" | "stopped" | "refused" | "not_run" */
  outcome: string;
  command: Record<string, unknown>;
  result?: string;
  error?: string;
  duration_ms?: number;
  terminal?: boolean;
  x?: number | null;
  y?: number | null;
  actual_pointer_x?: number | null;
  actual_pointer_y?: number | null;
  landed?: boolean | null;
  screen_width?: number | null;
  screen_height?: number | null;
}

/**
 * One real request to the model, and what came of it.
 *
 * One entry per request, not per command: a run that had to correct the model
 * twice made three calls, and the first two replies are the evidence for why.
 */
export interface ComputerTurn {
  turn: number;
  step: number;
  attempt: number;
  timestamp: number;
  reply_timestamp: number;
  provider: string;
  model: string;
  task: string;
  first_turn: boolean;
  prompt: string;
  prompt_attached: boolean;
  message_count: number;
  messages_meta: ComputerMessageMeta[];
  json_only: boolean;
  allowed_types: string[];
  screenshot_types: string[];
  screenshot_attached: boolean;
  image: string;
  image_meta: Partial<ComputerImageMeta>;
  image_withheld?: boolean;
  /** The user turn this request ended with, in full. */
  user_text: string;
  /**
   * The serialised request, when there was one.
   *
   * Optional rather than an empty object because an empty object is a claim:
   * it says "nothing was serialised" with the same confidence as a real summary
   * says what was, and the panel rendered both identically.
   */
  wire?: ComputerWire;
  raw: string;
  /**
   * Why the provider said the turn ended: `stop`, `length`, `tool_calls`,
   * `content_filter`.  Empty when it said nothing.
   *
   * This is what separates a reply with no text and no tool call that was
   * truncated by the completion ceiling from one that carried a call the parser
   * could not read.  Both arrive as empty text; only this says which it was.
   */
  stop_reason?: string;
  /**
   * The semantic history the model wrote, read from the `history` argument of
   * this reply's own tool call, and the exact text the *next* request will carry
   * as `History.txt`.
   *
   * Separate from `execution` on purpose: this is what the model said it issued,
   * that is what the machine did with it. Empty with a `history_error` means the
   * model wrote none the runner could accept — it is never invented.
   */
  history_note?: string;
  history_error?: string;
  /**
   * The native tool call this reply contained, with `arguments` still as the
   * provider's raw JSON string.
   *
   * Present on the normal path: a tool-calling model replies with a call and
   * `content: null`, so `raw` is legitimately empty while the turn did exactly
   * what it was asked to do. Without this the UI reports the commonest reply of
   * all as an empty one.
   */
  tool_call?: { name: string; arguments: string } | null;
  error: string;
  /**
   * How the request ended, when it ended badly.
   *
   * Split out from `error` because "the provider refused" and "the provider was
   * never reached" are different problems with different fixes, and one
   * flattened sentence cannot say which happened. `provider_reached` is the
   * whole distinction; the fields after it are the evidence behind it.
   */
  provider_reached?: boolean | null;
  http_status?: number;
  http_reason?: string;
  /** The provider's own explanation, extracted from its error body. */
  provider_error?: string;
  /** The error body verbatim, for when the extracted sentence is not enough. */
  provider_error_raw?: string;
  retry_after?: number | null;
  retry_attempts?: number;
  http_attempts?: ComputerHttpAttempt[];
  parse_ok: boolean;
  parse_error: string;
  command: Record<string, unknown>;
  execution: ComputerExecution;
  next_image: string;
  next_image_meta: Partial<ComputerImageMeta>;
}

/**
 * One refused HTTP attempt.
 *
 * A run that hit a 429 three times before giving up made three attempts, and
 * only the last one is visible in `http_status`. This is the list behind it, so
 * a bounded retry reads as "tried three times, then stopped" rather than
 * "failed once, for no stated reason".
 */
export interface ComputerHttpAttempt {
  attempt: number;
  provider: string;
  model: string;
  http_status: number;
  http_reason: string;
  provider_error: string;
  provider_error_raw: string;
  retry_after: number | null;
  retryable: boolean;
  reached: boolean;
}

/** The structured reason a run stopped, attached to the whole trace. */
export interface ComputerFailure {
  turn: number;
  provider: string;
  model: string;
  message: string;
  provider_reached: boolean | null;
  http_status: number;
  http_reason: string;
  provider_error: string;
  retry_after: number | null;
  retry_attempts: number;
  http_attempts: ComputerHttpAttempt[];
  final_result: string;
}

/** What the browser may know about a selectable computer-control provider. */
export interface ComputerProvider {
  name: string;
  label: string;
  model: string;
  configured: boolean;
}

export async function fetchComputerProviders(): Promise<{
  providers: ComputerProvider[];
  default: string;
}> {
  const res = await apiFetch("/ai/computer/providers");
  const data = (await res.json()) as { providers: ComputerProvider[]; default: string; error?: string };
  if (data.error) throw new Error(data.error);
  return { providers: data.providers ?? [], default: data.default };
}

/**
 * Point the next request at a different provider.
 *
 * The run keeps its task, its history and its step count; only who answers the
 * next call changes.  Takes effect on the next request, so a reply already in
 * flight is still attributed to the provider that produced it.
 */
export async function setComputerProvider(taskId: string, provider: string): Promise<{ provider: string; model: string }> {
  const res = await apiFetch(`/ai/computer/${taskId}/provider`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider }),
  });
  const data = (await res.json()) as { provider: string; model: string; error?: string };
  if (data.error) throw new Error(data.error);
  return { provider: data.provider, model: data.model };
}

export interface ComputerTrace {
  task_id: string;
  task: string;
  thread_id: string;
  status: string;
  message: string;
  url: string;
  step: number;
  started_at: number;
  finished_at: number;
  /** The provider the next request will use. */
  provider: string;
  /** Every provider the selector may offer, and whether it has a key. */
  selected_providers: ComputerProvider[];
  /** The provider and model that actually answered the last request. */
  last_provider: string;
  last_model: string;
  /**
   * Why the run stopped, field by field, or absent when it did not fail.
   *
   * Read off the turn that actually carries the error rather than off the last
   * turn, so a failure is attributed to the request that caused it. The panel
   * uses this instead of parsing `message`, because the whole point of these
   * fields is that the difference between a rate limit and an unreachable host
   * is not something a string can carry.
   */
  failure?: ComputerFailure;
  /**
   * `History.txt` as the next request carries it: one `{"history": ...}` object
   * per action, in the model's own words, newest last.
   *
   * Never contains coordinates, a tool name with its arguments, or a success or
   * failure verdict — those are the executor's, and they are in `turn.execution`.
   */
  history?: string;
  /** The executor's own per-attempt log, kept beside the history, never in it. */
  executor_facts?: string[];
  protocol: {
    first_turn_allowed: string[];
    after_screenshot_allowed: string[];
    after_screenshot_visible_target: string[];
    json_only: boolean;
  };
  turns: ComputerTurn[];
}

/**
 * The full run: the prompt, the screenshot, the verbatim reply, the parsed
 * command and what the executor did with it.
 *
 * The backend never puts an API key or a header in here, and it is the only
 * place the raw model output exists -- the status endpoint deliberately does
 * not carry it, so a failure that only shows up in the trace is only visible
 * here.
 */
export async function fetchComputerTrace(taskId: string): Promise<ComputerTrace> {
  const res = await apiFetch(`/ai/computer/${taskId}/trace`);
  const data = (await res.json()) as ComputerTrace & { error?: string };
  if (data.error) throw new Error(data.error);
  return data;
}
