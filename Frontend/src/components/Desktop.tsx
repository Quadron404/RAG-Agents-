import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import {
  Cpu,
  Database,
  Folder,
  Globe,
  HardDrive,
  Maximize2,
  Minimize2,
  Monitor,
  Power,
  X,
} from "lucide-react";

import { useCore } from "../core";
import { fmtBytes, fmtUptime } from "../lib/format";
import { haptic } from "../lib/haptics";
import { FilesPane } from "../views/Files";
import { TerminalPane } from "./TerminalPane";
import { VncScreen } from "./VncScreen";

/* ============================================================================
   RAG Agents desktop

   A desktop metaphor for the Computer page: a wallpaper, a set of app icons,
   and floating glass windows.  The point of the metaphor is that the live
   screen becomes an *app* rather than a tab, which is what lets it own the full
   height of the panel -- previously a tab strip sat above the stage, so the top
   of the remote Chrome (its tab strip and address bar) was never actually
   reachable on a laptop screen.

   Nothing here changes how the screen connects.  VncScreen is mounted exactly
   as it always was; the only concession is `controls="overlay"`, which floats
   its toolbar at the bottom of the stage so it stops covering the top of the
   remote browser.  The CDP screenshot view is gone from the UI entirely, since
   a still image of a browser is not a browser.
   ========================================================================= */

type AppId = "browser" | "terminal" | "files";

interface AppDef {
  id: AppId;
  label: string;
  /** Shown under the icon and in the window title. */
  blurb: string;
  Icon: typeof Globe;
  render: (running: boolean) => ReactNode;
}

const APPS: AppDef[] = [
  {
    id: "browser",
    label: "Browser",
    blurb: "Live Chrome",
    Icon: Globe,
    // The live screen *is* the browser app.  No address bar, no tabs, no
    // screenshot: the remote Chrome provides all of that, and a second set of
    // controls above it would only cover the part of it people need to click.
    render: (running) => <VncScreen running={running} controls="overlay" />,
  },
  {
    id: "terminal",
    label: "Terminal",
    blurb: "Remote shell",
    Icon: Cpu,
    render: () => <TerminalPane />,
  },
  {
    id: "files",
    label: "Files",
    blurb: "Workspace",
    Icon: Folder,
    render: () => <FilesPane embedded />,
  },
];

interface WinState {
  open: boolean;
  minimized: boolean;
  maximized: boolean;
  fullscreen: boolean;
  z: number;
  x: number;
  y: number;
  w: number;
  h: number;
  /** Set for one animation frame-batch after close, so the window can fade. */
  closing: boolean;
}

const INITIAL: Record<AppId, WinState> = {
  browser: { open: true, minimized: false, maximized: true, fullscreen: false, z: 3, x: 0, y: 0, w: 0, h: 0, closing: false },
  terminal: { open: false, minimized: false, maximized: false, fullscreen: false, z: 1, x: 0, y: 0, w: 0, h: 0, closing: false },
  files: { open: false, minimized: false, maximized: false, fullscreen: false, z: 2, x: 0, y: 0, w: 0, h: 0, closing: false },
};

const MENUBAR_H = 46;
const TITLE_H = 42;
const GAP = 8;

/** The geometry a maximised window occupies, so a drag can be measured from it. */
const maxedBox = (surface: { w: number; h: number }) => ({
  left: GAP,
  top: MENUBAR_H + 6,
  width: Math.max(0, surface.w - GAP * 2),
  height: Math.max(0, surface.h - MENUBAR_H - 14),
});

/**
 * Where a window goes when it is neither maximised nor full screen.
 *
 * `sized` is passed back in so a window that has already been moved or resized
 * returns to where the user left it; a window that has never been placed (the
 * Browser starts maximised, so it has no size yet) gets a sensible default.
 */
function restoreBox(surface: { w: number; h: number }, sized?: WinState, offset = 0) {
  const w = sized && sized.w > 0 ? sized.w : Math.min(1080, Math.max(420, Math.round(surface.w * 0.82)));
  const h = sized && sized.h > 0 ? sized.h : Math.min(760, Math.max(300, Math.round(surface.h * 0.78)));
  const clampX = (v: number) => Math.min(Math.max(GAP, v), Math.max(GAP, surface.w - w - GAP));
  const clampY = (v: number) => Math.min(Math.max(MENUBAR_H + GAP, v), Math.max(MENUBAR_H + GAP, surface.h - h - GAP));
  return {
    w,
    h,
    x: clampX(Math.round((surface.w - w) / 2) - offset),
    y: clampY(MENUBAR_H + Math.round((surface.h - MENUBAR_H - h) / 2) - offset),
  };
}

export function Desktop() {
  const computer = useCore((s) => s.computer);
  const sysinfo = useCore((s) => s.sysinfo);
  const computerStart = useCore((s) => s.computerStart);
  const computerStop = useCore((s) => s.computerStop);
  const running = computer.running;

  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const [wins, setWins] = useState<Record<AppId, WinState>>(INITIAL);
  const [active, setActive] = useState<AppId | null>("browser");
  const [surface, setSurface] = useState({ w: 1280, h: 800 });
  const [hint, setHint] = useState(true);

  /**
   * Which windows are currently showing their title bar in "bare" mode.
   *
   * The Browser window drops its bar out of the layout entirely when it is
   * maximised or full screen, so the remote Chrome reaches the top edge of the
   * panel.  The bar then fades back in when the pointer approaches the top edge
   * and fades out again once it has been left alone, so the window controls stay
   * reachable without permanently covering the remote browser's own tab strip.
   */
  const [chromeOn, setChromeOn] = useState<Record<AppId, boolean>>({
    browser: false,
    terminal: false,
    files: false,
  });
  const chromeTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (chromeTimer.current) window.clearTimeout(chromeTimer.current);
    },
    []
  );

  const revealChrome = useCallback((id: AppId, nearTop: boolean) => {
    if (chromeTimer.current) window.clearTimeout(chromeTimer.current);
    setChromeOn((s) => (nearTop ? (s[id] ? s : { ...s, [id]: true }) : s));
    chromeTimer.current = window.setTimeout(() => {
      setChromeOn((s) => (s[id] ? { ...s, [id]: false } : s));
    }, nearTop ? 2400 : 900);
  }, []);

  /* --- surface size, used to clamp and to size maximised windows ---------- */
  useLayoutEffect(() => {
    const el = surfaceRef.current;
    if (!el) return;
    const read = () => setSurface({ w: el.clientWidth, h: el.clientHeight });
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The first hint is enough; a permanent one is clutter.
  useEffect(() => {
    const t = setTimeout(() => setHint(false), 7000);
    return () => clearTimeout(t);
  }, []);

  const patch = useCallback((id: AppId, next: Partial<WinState>) => {
    setWins((s) => ({ ...s, [id]: { ...s[id], ...next } }));
  }, []);

  /** Raise a window to the front and pull it out of the dock. */
  const focusWindow = useCallback(
    (id: AppId) => {
      setActive(id);
      setWins((s) => {
        const top = Math.max(...Object.values(s).map((w) => w.z)) + 1;
        return { ...s, [id]: { ...s[id], z: top, minimized: false } };
      });
    },
    []
  );

  const openApp = useCallback(
    (id: AppId) => {
      haptic("medium");
      setWins((s) => {
        const top = Math.max(...Object.values(s).map((w) => w.z)) + 1;
        if (s[id].open) return { ...s, [id]: { ...s[id], z: top, minimized: false, closing: false } };
        // Offset by how many are already open, so two fresh windows do not
        // land in exactly the same spot.
        const offset = Object.values(s).filter((v) => v.open).length * 34;
        return { ...s, [id]: { ...s[id], open: true, minimized: false, closing: false, z: top, ...restoreBox(surface, s[id], offset) } };
      });
      setActive(id);
    },
    [surface]
  );

  /** Close with a beat of animation before the window is actually removed. */
  const closeApp = useCallback(
    (id: AppId) => {
      haptic("light");
      patch(id, { closing: true, minimized: false });
      setActive((cur) => (cur === id ? null : cur));
      setTimeout(() => {
        setWins((s) => ({ ...s, [id]: { ...s[id], open: false, closing: false } }));
      }, 200);
    },
    [patch]
  );

  const minimize = useCallback(
    (id: AppId) => {
      haptic("light");
      patch(id, { minimized: true });
      setActive((cur) => (cur === id ? null : cur));
    },
    [patch]
  );

  const toggleMax = useCallback(
    (id: AppId) => {
      haptic("light");
      setWins((s) => {
        // Restoring a window that was never placed (the Browser boots
        // maximised, so it has no saved geometry) needs a size first --
        // otherwise it would come back as a 0x0 sliver.
        const restore = s[id].maximized && s[id].w === 0 ? restoreBox(surface, s[id]) : null;
        return {
          ...s,
          [id]: {
            ...s[id],
            ...(restore ?? {}),
            maximized: !s[id].maximized,
            fullscreen: false,
            minimized: false,
          },
        };
      });
      setActive(id);
    },
    [surface]
  );

  const toggleFull = useCallback((id: AppId) => {
    haptic("medium");
    setWins((s) => ({ ...s, [id]: { ...s[id], fullscreen: !s[id].fullscreen, maximized: false, minimized: false } }));
    setActive(id);
  }, []);

  const restoreFromDock = useCallback(
    (id: AppId) => {
      haptic("light");
      focusWindow(id);
    },
    [focusWindow]
  );

  /* --- dragging ----------------------------------------------------------- */
  const startDrag = useCallback(
    (e: ReactPointerEvent, id: AppId) => {
      if (e.button !== 0) return;
      const startX = e.clientX;
      const startY = e.clientY;
      const current = wins[id];
      focusWindow(id);

      // Dragging a maximised window un-maximises it.  Measure the grab point
      // against the maximised frame first, otherwise the window teleports to
      // the centre on the first pointermove.
      const m = maxedBox(surface);
      const grabX = current.maximized ? Math.min(Math.max(0, e.clientX - m.left), m.width) : 0;
      const grabY = current.maximized ? Math.min(Math.max(0, e.clientY - m.top), TITLE_H) : 0;
      const base: WinState = current.maximized
        ? { ...current, ...restoreBox(surface, current), maximized: false, fullscreen: false }
        : current;
      if (current.maximized) {
        base.x = Math.min(
          Math.max(GAP, startX - grabX),
          Math.max(GAP, surface.w - base.w - GAP)
        );
        base.y = Math.min(
          Math.max(MENUBAR_H, startY - grabY),
          Math.max(MENUBAR_H, surface.h - TITLE_H)
        );
        patch(id, { maximized: false, fullscreen: false, x: base.x, y: base.y, w: base.w, h: base.h });
      }

      const move = (ev: PointerEvent) => {
        setWins((s) => ({
          ...s,
          [id]: {
            ...s[id],
            // Keep the title bar reachable: a window dragged off the top edge
            // is a window you cannot get back.
            x: Math.min(Math.max(-s[id].w + 120, base.x + ev.clientX - startX), surface.w - 120),
            y: Math.min(Math.max(MENUBAR_H, base.y + ev.clientY - startY), surface.h - TITLE_H),
          },
        }));
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    [focusWindow, patch, surface, wins]
  );

  const startResize = useCallback(
    (e: ReactPointerEvent, id: AppId) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      const startX = e.clientX;
      const startY = e.clientY;
      const base = wins[id];
      focusWindow(id);
      const move = (ev: PointerEvent) => {
        setWins((s) => ({
          ...s,
          [id]: {
            ...s[id],
            maximized: false,
            fullscreen: false,
            w: Math.max(360, Math.min(surface.w - 40, base.w + ev.clientX - startX)),
            h: Math.max(220, Math.min(surface.h - MENUBAR_H - 16, base.h + ev.clientY - startY)),
          },
        }));
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    [focusWindow, surface, wins]
  );

  /* --- system chips, unchanged from the old status bar -------------------- */
  const memAllocMb = Number(computer.memMb) || 0;
  const memUsedKb = Number(sysinfo?.mem?.used_kb) || 0;
  const memLabel = memAllocMb
    ? `${memAllocMb >= 1024 ? `${Math.round(memAllocMb / 1024)} GB` : `${memAllocMb} MB`}`
    : sysinfo?.mem?.total_kb
      ? fmtBytes(Number(sysinfo.mem.total_kb) * 1024)
      : "mem";
  const memUsedLabel = memAllocMb && memUsedKb ? fmtBytes(memUsedKb * 1024) : "";
  const sysCores = computer.cpus
    ? `${computer.cpus} ${computer.cpus === 1 ? "core" : "cores"}`
    : sysinfo?.cpu?.cores
      ? `${sysinfo.cpu.cores} cores`
      : "cpu";

  const anyFullscreen = useMemo(() => Object.values(wins).some((w) => w.fullscreen), [wins]);
  const dockApps = useMemo(() => APPS.filter((a) => wins[a.id].open), [wins]);

  return (
    <div className="desktop" ref={surfaceRef}>
      <div className="desktop__wall" aria-hidden />

      {/* ---- menu bar: the only permanent chrome above the desktop ---- */}
      <div className={`deskbar${anyFullscreen ? " deskbar--behind" : ""}`}>
        <div className="deskbar__brand">
          <span className="deskbar__mark" aria-hidden />
          <span className="deskbar__name">RAG Agents</span>
          <span className="deskbar__sub">Computer</span>
        </div>
        <div className="comp__spacer" />
        <div className="syschips">
          <span className="syschip syschip--hide-sm">
            <Cpu size={12} />
            {sysCores}
          </span>
          <span
            className="syschip syschip--hide-sm"
            title={
              memUsedKb
                ? `${memLabel} of memory (${fmtBytes(memUsedKb * 1024)} in use)`
                : `${memLabel} of memory available`
            }
          >
            <Database size={12} />
            {memLabel}
            {memUsedLabel ? <em className="syschip__sub">{memUsedLabel}</em> : null}
          </span>
          {sysinfo?.disk?.used !== undefined ? (
            <span className="syschip syschip--hide-sm">
              <HardDrive size={12} />
              {fmtBytes(sysinfo.disk.used)} / {fmtBytes(sysinfo.disk?.total ?? 0)}
            </span>
          ) : null}
          <span className="syschip">
            <Monitor size={12} />
            {sysinfo?.uptime ? `up ${fmtUptime(sysinfo.uptime)}` : running ? computer.msg || "online" : "offline"}
          </span>
        </div>
        <button
          className={`icon-btn${running ? " icon-btn--toggle" : ""}`}
          onClick={() => (running ? (haptic("medium"), computerStop()) : (haptic("medium"), computerStart()))}
          title={running ? "Power off computer" : "Boot the computer"}
          aria-label={running ? "Power off computer" : "Boot the computer"}
          style={running ? { color: "var(--green)" } : undefined}
        >
          <Power size={17} />
        </button>
      </div>

      {/* ---- app icons ---- */}
      <div className="deskicons" role="list" aria-label="Applications">
        {APPS.map((app) => {
          const w = wins[app.id];
          const isOpen = w.open;
          return (
            <button
              key={app.id}
              className={`deskicon${isOpen ? " deskicon--open" : ""}`}
              onClick={() => (isOpen && !w.minimized ? focusWindow(app.id) : openApp(app.id))}
              onDoubleClick={() => openApp(app.id)}
              role="listitem"
              aria-label={`${app.label} — ${app.blurb}`}
            >
              <span className="deskicon__tile">
                <app.Icon size={26} strokeWidth={1.7} />
              </span>
              <span className="deskicon__label">{app.label}</span>
            </button>
          );
        })}
      </div>

      {hint && !anyFullscreen ? (
        <div className="deskhint" role="note">
          Open an app above, or double-click an icon. Drag a title bar to move a window.
        </div>
      ) : null}

      {/* ---- windows ---- */}
      {APPS.map((app) => {
        const w = wins[app.id];
        if (!w.open) return null;
        const isActive = active === app.id && !w.minimized;
        // The Browser gives up its title bar to the screen as soon as it fills
        // the panel; the other apps keep theirs, because their content is a
        // document rather than a screen with its own chrome at the top.
        const bare = app.id === "browser" && (w.maximized || w.fullscreen);
        const style: CSSProperties = w.fullscreen
          ? { zIndex: 9000 + w.z }
          : w.maximized
            ? { zIndex: w.z, ...maxedBox(surface) }
            : { zIndex: w.z, left: w.x, top: w.y, width: w.w, height: w.h };

        return (
          <section
            key={app.id}
            className={[
              "dwin",
              `dwin--${app.id}`,
              isActive ? "dwin--active" : "",
              w.minimized ? "dwin--min" : "",
              w.closing ? "dwin--closing" : "",
              w.maximized ? "dwin--max" : "",
              w.fullscreen ? "dwin--full" : "",
              bare ? "dwin--bare" : "",
              bare && chromeOn[app.id] ? "dwin--chrome" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            style={style}
            aria-label={`${app.label} window`}
            aria-hidden={w.minimized || undefined}
            onPointerDown={() => setActive(app.id)}
            onPointerMove={(e) => {
              if (!bare) return;
              const top = e.currentTarget.getBoundingClientRect().top;
              revealChrome(app.id, e.clientY - top <= 72);
            }}
            onPointerLeave={() => {
              if (!bare) return;
              if (chromeTimer.current) window.clearTimeout(chromeTimer.current);
              chromeTimer.current = window.setTimeout(() => {
                setChromeOn((s) => (s[app.id] ? { ...s, [app.id]: false } : s));
              }, 500);
            }}
          >
            <header
              className="dwin__bar"
              onPointerDown={(e) => startDrag(e, app.id)}
              onDoubleClick={() => toggleMax(app.id)}
            >
              <div className="dwin__lights">
                <button
                  className="dwin__light dwin__light--close"
                  onClick={() => closeApp(app.id)}
                  title={`Close ${app.label}`}
                  aria-label={`Close ${app.label}`}
                >
                  <X size={11} />
                </button>
                <button
                  className="dwin__light dwin__light--min"
                  onClick={() => minimize(app.id)}
                  title={`Minimise ${app.label}`}
                  aria-label={`Minimise ${app.label}`}
                >
                  <Minimize2 size={11} />
                </button>
                <button
                  className="dwin__light dwin__light--max"
                  onClick={() => toggleMax(app.id)}
                  title={w.maximized ? `Restore ${app.label}` : `Maximise ${app.label}`}
                  aria-label={w.maximized ? `Restore ${app.label}` : `Maximise ${app.label}`}
                >
                  <Maximize2 size={10} />
                </button>
              </div>
              <div className="dwin__title">
                <app.Icon size={13} />
                <span>{app.label}</span>
                <em>{app.blurb}</em>
              </div>
              <div className="dwin__acts">
                <button
                  className="icon-btn"
                  onClick={() => toggleFull(app.id)}
                  title={w.fullscreen ? "Leave full screen" : "Full screen"}
                  aria-label={w.fullscreen ? "Leave full screen" : "Full screen"}
                  aria-pressed={w.fullscreen}
                >
                  {w.fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
                </button>
              </div>
            </header>
            <div className="dwin__body">{app.render(running)}</div>
            {!w.maximized && !w.fullscreen ? (
              <div
                className="dwin__grip"
                onPointerDown={(e) => startResize(e, app.id)}
                role="separator"
                aria-label="Resize window"
              />
            ) : null}
          </section>
        );
      })}

      {/* ---- dock: restores minimised windows, mirrors real desktops ---- */}
      <div className={`deskdock${anyFullscreen ? " deskdock--behind" : ""}`}>
        {dockApps.length === 0 ? <span className="deskdock__empty" /> : null}
        {dockApps.map((app) => (
          <button
            key={app.id}
            className={`deskdock__item${active === app.id && !wins[app.id].minimized ? " deskdock__item--on" : ""}`}
            onClick={() => restoreFromDock(app.id)}
            title={wins[app.id].minimized ? `Restore ${app.label}` : app.label}
            aria-label={wins[app.id].minimized ? `Restore ${app.label}` : app.label}
          >
            <app.Icon size={17} strokeWidth={1.8} />
            <span>{app.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
