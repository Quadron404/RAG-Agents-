import { api, wsUrl } from "../core";

/* ============================================================================
   Where the live screen lives.

   Two routes, one implementation of the viewer:

   tunnel  (production)
     The Computer view opens a WebSocket straight to
     wss://computer.<domain>/websockify.  Cloudflare Access authenticates it and
     the tunnel forwards it to websockify on the remote machine's loopback.
     The backend is not in the data path at all, so no frame ever touches the
     API process.

   bridge  (development / tunnel outage)
     The backend relays raw RFB over its own WebSocket at /ws/screen.  Same
     protocol, same noVNC client, just one extra hop.  Convenient locally and
     the reason the screen still works when Cloudflare is down -- but it is
     never the production route, because it is not covered by Access.

   The choice is a *server* setting (COMPUTER_WS_URL) surfaced through
   /screen/config, so the hostname lives in exactly one place and the bundle
   never has to be rebuilt when it changes.
   ========================================================================== */

export type ScreenMode = "tunnel" | "bridge";

export interface ScreenConfig {
  mode: ScreenMode;
  /** The authenticated, Cloudflare-proxied endpoint. Empty in bridge mode. */
  wsUrl: string;
  /** Backend path used only in bridge mode. */
  bridgePath: string;
  /** noVNC negotiates the binary subprotocol; websockify serves it. */
  wsProtocols: string[];
}

const FALLBACK: ScreenConfig = {
  mode: "bridge",
  wsUrl: "",
  bridgePath: "/ws/screen",
  wsProtocols: ["binary"],
};

let cached: ScreenConfig | null = null;
let inflight: Promise<ScreenConfig> | null = null;

/** Fetch (once) how the viewer should connect. Falls back to the bridge. */
export function loadScreenConfig(): Promise<ScreenConfig> {
  if (cached) return Promise.resolve(cached);
  // De-duplicate the concurrent calls that happen when two views mount at once.
  if (inflight) return inflight;
  inflight = fetch(api("/screen/config"))
    .then((r) => (r.ok ? (r.json() as Promise<ScreenConfig>) : FALLBACK))
    .then((cfg) => {
      cached = {
        mode: cfg?.mode === "tunnel" && cfg?.wsUrl ? "tunnel" : "bridge",
        wsUrl: cfg?.wsUrl || "",
        bridgePath: cfg?.bridgePath || FALLBACK.bridgePath,
        wsProtocols: cfg?.wsProtocols?.length ? cfg.wsProtocols : FALLBACK.wsProtocols,
      };
      return cached;
    })
    .catch(() => FALLBACK)
    .then((cfg) => {
      cached = cfg;
      inflight = null;
      return cfg;
    });
  return inflight;
}

/** The WebSocket URL noVNC should open, given the resolved config. */
export function screenSocketUrl(cfg: ScreenConfig): string {
  return cfg.mode === "tunnel" ? cfg.wsUrl : wsUrl(cfg.bridgePath);
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
    chromium?: boolean;
    restarts?: Record<string, number>;
    error?: string;
  };
}

export async function fetchScreenStatus(): Promise<ScreenStatus> {
  const res = await fetch(api("/screen/status"));
  if (!res.ok) throw new Error(`screen status ${res.status}`);
  return (await res.json()) as ScreenStatus;
}

/** Ask the remote machine to bring its display stack up. */
export async function ensureDisplay(): Promise<void> {
  await fetch(api("/screen/ensure"), { method: "POST" });
}

/** Restart the remote browser; the screen then follows the real state. */
export async function restartRemoteBrowser(): Promise<void> {
  await fetch(api("/screen/browser/restart"), { method: "POST" });
}
