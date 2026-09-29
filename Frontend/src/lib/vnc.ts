import type RFB from "@novnc/novnc";
import { loadScreenConfig, screenSocketUrl, type ScreenConfig } from "./screen";

/* noVNC is a few hundred kB of RFB client, and most sessions never open the
   Computer view.  Load it on demand so it stays out of the first paint. */
let rfbCtor: Promise<typeof RFB> | null = null;
function loadRfb(): Promise<typeof RFB> {
  if (!rfbCtor) rfbCtor = import("@novnc/novnc").then((m) => m.default);
  return rfbCtor;
}

/* ============================================================================
   Live screen session.

   The pixels come straight from the remote machine's VNC server.  noVNC
   speaks RFB over a WebSocket, the framebuffer is never decoded, resampled or
   cached on the way, and nothing in between passes it to the model.  Where
   that WebSocket points is decided by /screen/config: the authenticated
   Cloudflare route in production, this app's own relay while developing.
   ========================================================================== */

export type VncState = "connecting" | "connected" | "disconnected" | "reconnecting";

export interface VncStats {
  fps: number;
  latencyMs: number;
  frames: number;
  bytes: number;
  width: number;
  height: number;
  since: number;
}

export interface VncSessionOptions {
  onState: (state: VncState, detail?: string) => void;
  onStats: (stats: VncStats) => void;
  /** Lets the UI say which route it took, e.g. "quick tunnel" vs "relay". */
  onRoute?: (mode: ScreenConfig["mode"]) => void;
}

const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 8000;
const STATS_WINDOW_MS = 2000;

export class VncSession {
  private rfb: RFB | null = null;
  private target: HTMLElement;
  private opts: VncSessionOptions;
  private stopped = false;
  private attempt = 0;
  private openToken = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private statsTimer: ReturnType<typeof setInterval> | null = null;
  private frameTimes: number[] = [];
  private lastInputAt = 0;
  private bytes = 0;
  private connectedAt = 0;
  private latencyMs = 0;
  private width = 0;
  private height = 0;
  private keyGuard: ((e: KeyboardEvent) => void) | null = null;
  private inputWatchers: Array<() => void> = [];
  private scrollGuard: (() => void) | null = null;
  private refitQueued = false;
  /** The socket URL currently in use, so a tunnel move can be detected. */
  private activeUrl = "";
  private focusOnPointer: ((e: PointerEvent) => void) | null = null;

  constructor(target: HTMLElement, opts: VncSessionOptions) {
    this.target = target;
    this.opts = opts;
  }

  get state(): VncState {
    return this.lastState;
  }

  private lastState: VncState = "connecting";

  private setState(state: VncState, detail?: string) {
    this.lastState = state;
    this.opts.onState(state, detail);
  }

  start(): void {
    this.stopped = false;
    this.open();
  }

  /** Force a fresh connection now (used by the Reconnect control). */
  reconnectNow(): void {
    this.clearRetry();
    this.teardown();
    this.attempt = 0;
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clearRetry();
    this.teardown();
    this.setState("disconnected");
  }

  /** Re-run noVNC's viewport fit, e.g. after the container changed size. */
  refit(): void {
    if (!this.rfb) return;
    // Assigning scaleViewport re-runs noVNC's autoscale against the element's
    // current box, which is what makes window resizing behave.
    this.rfb.scaleViewport = true;
    if (this.refitQueued) return;
    this.refitQueued = true;
    // noVNC applies the new scale through its own render queue, and a
    // fractional contain-fit can leave the viewport scrolled by a fraction of a
    // pixel.  Re-pin across two frames so what ends up on screen is the whole
    // framebuffer rather than the bottom of it.
    requestAnimationFrame(() => {
      this.refitQueued = false;
      this.pinViewport();
      requestAnimationFrame(() => this.pinViewport());
    });
  }

  /** noVNC's scrollable viewport container, which is the RFB target's child. */
  private viewport(): HTMLElement | null {
    const el = this.target.firstElementChild;
    return el instanceof HTMLElement ? el : null;
  }

  /**
   * Force the viewport back to its origin.
   *
   * The viewport is sized to hold the whole framebuffer, so any scroll offset on
   * it is by definition hiding part of the remote screen.  This is the belt to
   * the CSS `overflow: hidden` braces: even if the container is scrollable, the
   * top-left of the framebuffer stays on screen.
   */
  private pinViewport(): void {
    const vp = this.viewport();
    if (vp && (vp.scrollTop !== 0 || vp.scrollLeft !== 0)) {
      vp.scrollTop = 0;
      vp.scrollLeft = 0;
    }
  }

  private clearRetry(): void {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  private teardown(): void {
    if (this.statsTimer) {
      clearInterval(this.statsTimer);
      this.statsTimer = null;
    }
    for (const off of this.inputWatchers) off();
    this.inputWatchers = [];
    if (this.scrollGuard) {
      this.scrollGuard();
      this.scrollGuard = null;
    }
    if (this.keyGuard) {
      this.target.removeEventListener("keydown", this.keyGuard, true);
      this.keyGuard = null;
    }
    if (this.focusOnPointer) {
      this.target.removeEventListener("pointerdown", this.focusOnPointer, true);
      this.focusOnPointer = null;
    }
    const rfb = this.rfb;
    this.rfb = null;
    if (rfb) {
      try {
        rfb.disconnect();
      } catch {
        /* already gone */
      }
    }
    this.target.replaceChildren();
  }

  private open(cfg?: ScreenConfig): void {
    if (this.stopped) return;
    this.setState(this.attempt === 0 ? "connecting" : "reconnecting");
    this.teardown();
    // A newer open() (or a stop()) can land while the chunk is in flight.
    const token = ++this.openToken;
    void Promise.all([loadRfb(), cfg ? Promise.resolve(cfg) : loadScreenConfig()])
      .then(([RFB, resolved]) => {
        if (this.stopped || token !== this.openToken) return;
        this.opts.onRoute?.(resolved.mode);
        this.activeUrl = screenSocketUrl(resolved);
        this.attach(RFB, resolved);
      })
      .catch((err: unknown) => {
        this.scheduleRetry(err instanceof Error ? err.message : "could not load the screen client");
      });
  }

  /**
   * Follow the server to a new origin.
   *
   * Called when the tunnel watcher sees /screen/config advertise a different
   * WebSocket URL, which happens whenever the Quick Tunnel is handed a new
   * hostname.  Reconnecting straight away -- rather than waiting for the
   * connection to time out and the retry backoff to climb past the config cache
   * -- is what makes a restarted tunnel recover in about a second instead of
   * about half a minute.
   *
   * Returns true if it actually moved, so the caller can log/report it.
   */
  syncTo(cfg: ScreenConfig): boolean {
    if (this.stopped) return false;
    const next = screenSocketUrl(cfg);
    if (!next || next === this.activeUrl) return false;
    this.opts.onRoute?.(cfg.mode);
    this.clearRetry();
    this.teardown();
    this.attempt = 0;
    this.open(cfg);
    return true;
  }

  private attach(RFB: typeof import("@novnc/novnc").default, cfg: ScreenConfig): void {
    const url = screenSocketUrl(cfg);
    let rfb: RFB;
    try {
      rfb = new RFB(this.target, url, { shared: true, wsProtocols: cfg.wsProtocols });
    } catch (err) {
      this.scheduleRetry(err instanceof Error ? err.message : "connection failed");
      return;
    }
    this.rfb = rfb;

    // noVNC's viewport is scrollable by default (overflow: auto).  Any scroll
    // offset on it hides part of the framebuffer, and the usual way one appears
    // is a wheel gesture landing on the screen at the same time as it is being
    // forwarded to the remote machine.  Snap it back to the origin instead.
    const vp = this.viewport();
    if (vp) {
      const onScroll = () => this.pinViewport();
      vp.addEventListener("scroll", onScroll, { passive: true });
      this.scrollGuard = () => vp.removeEventListener("scroll", onScroll);
    }

    rfb.viewOnly = false;
    rfb.clipViewport = false;
    rfb.scaleViewport = true;   // fit the framebuffer to whatever the UI gives us
    rfb.resizeSession = false;  // the remote machine owns its resolution
    rfb.showDotCursor = true;
    rfb.qualityLevel = 6;
    rfb.compressionLevel = 2;
    rfb.background = "#0b0d12";

    rfb.addEventListener("connect", () => {
      this.attempt = 0;
      this.connectedAt = Date.now();
      this.frameTimes = [];
      this.bytes = 0;
      this.latencyMs = 0;
      this.setState("connected");
      this.startStats();
      this.pinViewport();
      // A click is the natural moment to take keyboard input; make the very
      // first keystroke work without an extra tab-stop.
      try {
        rfb.focus({ preventScroll: true });
      } catch {
        /* focus is best effort */
      }
    });

    rfb.addEventListener("disconnect", (ev) => {
      const detail = (ev as CustomEvent<{ clean?: boolean }>).detail;
      if (this.stopped) {
        this.setState("disconnected");
        return;
      }
      this.scheduleRetry(detail?.clean ? "closed by server" : "connection lost");
    });

    rfb.addEventListener("securityfailure", (ev) => {
      const detail = (ev as CustomEvent<{ reason?: string }>).detail;
      this.scheduleRetry(detail?.reason || "VNC security negotiation failed");
    });

    rfb.addEventListener("credentialsrequired", () => {
      // Not expected: the route in front of the screen authenticates the
      // *user*, and x11vnc itself is loopback-only with no password.
      this.scheduleRetry("this screen needs a password");
    });

    this.installFrameCounter(rfb);
    this.installInputGuards();
  }

  /**
   * Count real FramebufferUpdate messages so the debug readout reports network
   * frames rather than a guessed number, and time input -> first painted frame.
   */
  private installFrameCounter(rfb: RFB): void {
    const inst = rfb as unknown as Record<string, unknown>;
    const proto = Object.getPrototypeOf(rfb) as Record<string, (...a: unknown[]) => unknown>;
    const original = proto._normalMsg;
    if (typeof original !== "function") return;
    const self = this;
    inst._normalMsg = function patched(this: unknown, ...args: unknown[]) {
      const result = original.apply(this, args);
      const msgType = (this as { _msg?: number })._msg;
      if (result === true && msgType === 0) {
        const now = performance.now();
        self.onFrame(now);
      }
      return result;
    };
  }

  private onFrame(now: number): void {
    this.frameTimes.push(now);
    if (this.frameTimes.length > 600) this.frameTimes.shift();
    if (this.lastInputAt) {
      this.latencyMs = Math.max(0, now - this.lastInputAt);
      this.lastInputAt = 0;
    }
  }

  /**
   * noVNC does not call preventDefault, so the browser would still act on
   * F5, Ctrl+R, Ctrl+W, F11 and friends and take the session down with it.
   * Suppressing those defaults while letting the event reach noVNC keeps
   * function keys and modifier combinations usable inside the VM.
   */
  private installInputGuards(): void {
    const guard = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      const isFunctionKey = /^F\d{1,2}$/.test(e.key);
      const hasModifier = e.ctrlKey || e.metaKey || e.altKey;
      const risky =
        isFunctionKey ||
        hasModifier ||
        e.key === "Escape" ||
        e.key === "Backspace" ||
        e.key === " " ||
        e.key === "PrintScreen";
      if (risky) e.preventDefault();
    };
    this.keyGuard = guard;
    this.target.addEventListener("keydown", guard, true);

    // noVNC only receives key events while its own element holds focus.  After a
    // reconnect, a click anywhere on the screen, or the browser moving focus
    // because the user tabbed away and came back, the first keystrokes would
    // otherwise go to RAG Agents instead of the remote Chrome.  Taking focus on
    // pointer-down is what makes "click, then type" work the way people expect.
    const takeFocus = (e: PointerEvent) => {
      if (e.button !== 0) return;
      // Let noVNC's own handler process the click first; focus afterwards.
      window.setTimeout(() => {
        if (this.stopped || !this.rfb) return;
        try {
          this.rfb.focus({ preventScroll: true });
        } catch {
          /* focus is best effort */
        }
      }, 0);
    };
    this.focusOnPointer = takeFocus;
    this.target.addEventListener("pointerdown", takeFocus, true);

    const mark = () => {
      this.lastInputAt = performance.now();
    };
    const events: Array<keyof HTMLElementEventMap> = ["mousedown", "wheel", "keydown"];
    for (const name of events) {
      const handler = () => mark();
      this.target.addEventListener(name, handler, true);
      this.inputWatchers.push(() => this.target.removeEventListener(name, handler, true));
    }
  }

  private startStats(): void {
    if (this.statsTimer) clearInterval(this.statsTimer);
    this.statsTimer = setInterval(() => {
      const now = performance.now();
      const cutoff = now - STATS_WINDOW_MS;
      while (this.frameTimes.length && this.frameTimes[0] < cutoff) this.frameTimes.shift();
      const fps = this.frameTimes.length ? (this.frameTimes.length * 1000) / (now - this.frameTimes[0] || 1) : 0;
      const rfb = this.rfb as unknown as { _display?: { _fbWidth?: number; _fbHeight?: number } } | null;
      this.width = rfb?._display?._fbWidth ?? this.width;
      this.height = rfb?._display?._fbHeight ?? this.height;
      this.opts.onStats({
        fps: Math.round(fps * 10) / 10,
        latencyMs: Math.round(this.latencyMs),
        frames: this.frameTimes.length,
        bytes: this.bytes,
        width: this.width,
        height: this.height,
        since: this.connectedAt,
      });
    }, 1000);
  }

  private scheduleRetry(detail: string): void {
    this.clearRetry();
    if (this.stopped) return;
    this.teardown();
    this.attempt += 1;
    this.setState("reconnecting", detail);
    // Exponential backoff with jitter so several open tabs do not stampede the
    // bridge at the same instant after a VM restart.
    const base = Math.min(RETRY_MIN_MS * 2 ** (this.attempt - 1), RETRY_MAX_MS);
    const wait = base + Math.random() * Math.min(base, 1000);
    this.retryTimer = setTimeout(() => this.open(), wait);
  }
}
