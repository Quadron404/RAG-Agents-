import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Gauge,
  Loader2,
  Lock,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  WifiOff,
} from "lucide-react";
import { haptic } from "../lib/haptics";
import {
  ensureDisplay,
  fetchScreenStatus,
  restartRemoteBrowser,
  type ScreenMode,
  type ScreenStatus,
} from "../lib/screen";
import { VncSession, type VncState, type VncStats } from "../lib/vnc";
import { useCore } from "../core";

type Phase = "boot" | "live" | "error";

const STATE_LABEL: Record<VncState, string> = {
  connecting: "CONNECTING",
  connected: "CONNECTED",
  disconnected: "DISCONNECTED",
  reconnecting: "RECONNECTING",
};

const DEBUG_KEY = "rag.vnc.debug";

/**
 * The real remote screen: a noVNC client showing the live framebuffer of the
 * Google Chrome running on the remote machine, driven by the user's real mouse
 * and keyboard.
 *
 * Nothing here renders a stand-in browser, and the framebuffer never passes
 * through the backend or the model.  Where the WebSocket points is decided by
 * the server: the authenticated Cloudflare route in production, this app's own
 * relay while developing.
 */
export function VncScreen({ running }: { running: boolean }) {
  const computerMsg = useCore((s) => s.computer.msg);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const sessionRef = useRef<VncSession | null>(null);
  const [state, setState] = useState<VncState>("connecting");
  const [detail, setDetail] = useState("");
  const [mode, setMode] = useState<ScreenMode>("bridge");
  const [stats, setStats] = useState<VncStats | null>(null);
  const [reconnects, setReconnects] = useState(0);
  const [showStats, setShowStats] = useState<boolean>(() => {
    try {
      return localStorage.getItem(DEBUG_KEY) === "1" || new URLSearchParams(location.search).has("vncdebug");
    } catch {
      return false;
    }
  });
  const [backend, setBackend] = useState<ScreenStatus | null>(null);
  const [busy, setBusy] = useState(false);

  // 1) keep the live pipeline attached while the Computer view is on screen
  useEffect(() => {
    const host = hostRef.current;
    if (!host || !running) return;
    const session = new VncSession(host, {
      onState: (next, info) => {
        // Count drops so a flapping remote machine is visible instead of just
        // looking like a slow connect.
        setState((prev) => {
          if (prev === "connected" && (next === "reconnecting" || next === "disconnected")) setReconnects((n) => n + 1);
          return next;
        });
        setDetail(info ?? "");
      },
      onStats: setStats,
      onRoute: setMode,
    });
    sessionRef.current = session;
    session.start();
    return () => {
      session.stop();
      sessionRef.current = null;
    };
  }, [running]);

  // 2) the framebuffer is scaled to the element, so refit whenever it changes
  useEffect(() => {
    const host = hostRef.current;
    if (!host || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => sessionRef.current?.refit());
    ro.observe(host);
    return () => ro.disconnect();
  }, [running]);

  // 3) independent health poll: shows real state even mid-reconnect
  const poll = useCallback(async () => {
    if (!running) return;
    try {
      setBackend(await fetchScreenStatus());
    } catch {
      setBackend((prev) => (prev ? { ...prev, listening: false } : prev));
    }
  }, [running]);

  useEffect(() => {
    if (!running) return;
    void poll();
    const id = setInterval(poll, 4000);
    return () => clearInterval(id);
  }, [poll, running]);

  useEffect(() => {
    try {
      localStorage.setItem(DEBUG_KEY, showStats ? "1" : "0");
    } catch {
      /* private mode */
    }
  }, [showStats]);

  const toggleStats = () => {
    haptic("light");
    setShowStats((v) => !v);
  };

  const reconnect = () => {
    haptic("medium");
    setDetail("");
    sessionRef.current?.reconnectNow();
  };

  const wakeDisplay = async () => {
    setBusy(true);
    haptic("medium");
    try {
      await ensureDisplay();
    } finally {
      setTimeout(() => {
        setBusy(false);
        void poll();
        sessionRef.current?.reconnectNow();
      }, 1200);
    }
  };

  const restartBrowser = async () => {
    setBusy(true);
    haptic("heavy");
    try {
      await restartRemoteBrowser();
    } finally {
      setTimeout(() => {
        setBusy(false);
        void poll();
      }, 1500);
    }
  };

  const phase: Phase = !running ? "boot" : state === "connected" ? "live" : "error";
  const desktop = backend?.display;
  const res = stats && stats.width ? `${stats.width}×${stats.height}` : desktop?.size ?? "";

  return (
    <div className="vncscreen">
      <div className="vnc__bar">
        {/* The route is user-visible on purpose: it is the difference between
            going out through a published tunnel and going through the in-app
            relay, and "which one am I on?" should never be a guess.  Both are
            session-gated; this is about the path, not the permission. */}
        <span
          className={`vnc__route vnc__route--${mode}`}
          title={
            mode === "tunnel"
              ? "Reaching the screen through this app's public tunnel, which requires your session"
              : "Relayed by this app's backend — no tunnel involved"
          }
        >
          {mode === "tunnel" ? <ShieldCheck size={14} /> : <Lock size={14} />}
          {mode === "tunnel" ? "Quick tunnel" : "Local relay"}
        </span>

        <div className={`vnc__status vnc__status--${state}`} data-state={state}>
          <i className="vnc__dot" />
          {STATE_LABEL[state]}
        </div>

        <div className="comp__spacer" />

        {res ? <span className="vnc__meta">{res}</span> : null}
        {reconnects > 0 ? (
          <span className="vnc__meta vnc__meta--warn" title="The screen dropped this many times this session">
            {reconnects} drop{reconnects === 1 ? "" : "s"}
          </span>
        ) : null}
        {desktop && desktop.chromium === false && state === "connected" ? (
          <span className="vnc__meta vnc__meta--warn">no browser running</span>
        ) : null}

        <button
          className={`icon-btn${showStats ? " icon-btn--toggle" : ""}`}
          onClick={toggleStats}
          title="Toggle stream diagnostics"
          aria-label="Toggle stream diagnostics"
          aria-pressed={showStats}
        >
          <Activity size={15} />
        </button>
        <button className="icon-btn" onClick={reconnect} title="Reconnect the screen" aria-label="Reconnect the screen">
          <RefreshCw size={16} />
        </button>
      </div>

      {showStats && stats ? (
        <div className="vnc__stats">
          <Gauge size={12} />
          <span>{stats.fps.toFixed(1)} fps</span>
          <span>{stats.latencyMs} ms</span>
          <span>{stats.frames} frames/2s</span>
          <span>{(stats.bytes / 1024).toFixed(0)} KiB window</span>
          <span>{mode}</span>
        </div>
      ) : null}

      <div className={`vnc__stage vnc__stage--${phase}`}>
        <div className="vnc__screen" ref={hostRef} />

        {phase === "boot" ? (
          <div className="vnc__overlay">
            <Loader2 size={26} className="spin" />
            <div className="vnc__overlayTitle">Computer offline</div>
            <div className="vnc__overlaySub">{computerMsg || "start the computer to see its real screen"}</div>
          </div>
        ) : null}

        {phase === "error" ? (
          <div className="vnc__overlay">
            {state === "reconnecting" ? (
              <Loader2 size={26} className="spin" />
            ) : (
              <WifiOff size={26} />
            )}
            <div className="vnc__overlayTitle">{STATE_LABEL[state]}</div>
            <div className="vnc__overlaySub">
              {detail || (state === "connecting" ? "opening the screen…" : "the screen is not answering")}
            </div>
            {mode === "tunnel" && state === "disconnected" ? (
              <div className="vnc__overlayNote">
                If this is the first visit, you may need to sign in again.
              </div>
            ) : null}
            {backend && !backend.listening ? (
              <div className="vnc__overlayNote">
                {backend.note || `nothing is listening on the screen port (${backend.port || "—"})`}
              </div>
            ) : null}
            <div className="vnc__overlayActions">
              <button className="btn" onClick={reconnect}>
                <RefreshCw size={15} /> Reconnect
              </button>
              <button className="btn" onClick={wakeDisplay} disabled={busy}>
                {busy ? <Loader2 size={15} className="spin" /> : <ShieldCheck size={15} />} Wake screen
              </button>
            </div>
          </div>
        ) : null}

        {phase === "live" ? (
          <>
            <div className="vnc__hint">Click the screen to type · Esc for the remote machine</div>
            <button className="vnc__restart" onClick={restartBrowser} disabled={busy} title="Restart the remote browser">
              {busy ? <Loader2 size={14} className="spin" /> : <RotateCcw size={14} />} Restart browser
            </button>
          </>
        ) : null}
      </div>
    </div>
  );
}
