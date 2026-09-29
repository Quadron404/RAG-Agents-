import { useEffect, useRef, useState, type ReactNode } from "react";
import { Activity, Check, Cpu, Database, HardDrive, Monitor, Palette, Server, Zap } from "lucide-react";

import { useCore } from "../core";
import type { SysInfo } from "../store";
import { fmtBytes, fmtUptime } from "../lib/format";
import { haptic } from "../lib/haptics";
import { WALLPAPERS, previewCss } from "../lib/wallpapers";

/**
 * Settings.
 *
 * Two sections, and the split is the point: **System** reports on the remote
 * Codespace, **Personalization** changes the desktop in front of the user.  They
 * are kept visually distinct because mixing them makes it ambiguous whether a
 * number describes your laptop or the machine the browser is actually running on.
 *
 * Everything in System comes from the agent's `/sysinfo`, read through the same
 * authenticated backend route the menu bar already uses -- this screen adds no
 * endpoint, no websocket and no polling of its own.  The store refreshes
 * `sysinfo` on a timer that already existed, so opening this window costs
 * nothing and a closed one costs nothing.
 *
 * The one number that cannot simply be read is CPU usage.  `/sysinfo` reports
 * cumulative jiffies, so a single sample is a lifetime average and says nothing
 * about *now*.  Differencing two samples is the honest way to get an
 * instantaneous figure, which is what the hook below does: it keeps the previous
 * sample in a ref, so no extra state is stored and no re-render happens unless
 * the number actually moved.
 */

const SECTIONS = [
  { id: "system", label: "System", icon: Server },
  { id: "personalization", label: "Personalization", icon: Palette },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

/** Live CPU load, differenced from consecutive cumulative samples. */
function useCpuLoad(sys: SysInfo | null) {
  const prev = useRef<{ idle: number; total: number } | null>(null);
  const [pct, setPct] = useState<number | null>(null);

  useEffect(() => {
    const cpu = sys?.cpu;
    if (!cpu || cpu.idle === undefined || cpu.total === undefined) {
      setPct(null);
      return;
    }
    const before = prev.current;
    prev.current = { idle: cpu.idle, total: cpu.total };
    if (!before) return;
    const dTotal = cpu.total - before.total;
    const dIdle = cpu.idle - before.idle;
    // A counter reset, or two samples too close together, can make the delta
    // zero or negative.  Leaving the previous value up is more honest than
    // inventing one.
    if (dTotal <= 0) return;
    setPct(Math.max(0, Math.min(100, Math.round((1 - dIdle / dTotal) * 100))));
  }, [sys]);

  return pct;
}

function Meter({
  icon,
  label,
  used,
  total,
  usedLabel,
  freeLabel,
}: {
  icon: ReactNode;
  label: string;
  used: number;
  total: number;
  usedLabel: string;
  freeLabel: string;
}) {
  const pct = total > 0 ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
  const tone = pct > 88 ? "var(--red)" : pct > 70 ? "var(--amber)" : "var(--accent)";
  return (
    <div className="set-meter">
      <div className="set-meter__head">
        <span className="set-meter__icon">{icon}</span>
        <span className="set-meter__label">{label}</span>
        <span className="set-meter__pct">{pct.toFixed(0)}%</span>
      </div>
      <div className="set-meter__track">
        <i style={{ width: `${pct}%`, background: tone }} />
      </div>
      <div className="set-meter__foot">
        <span>{usedLabel} used</span>
        <span>{freeLabel} free</span>
      </div>
    </div>
  );
}

function Row({ label, value, mono = false }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="set-row">
      <span className="set-row__k">{label}</span>
      <span className={`set-row__v${mono ? " mono" : ""}`}>{value}</span>
    </div>
  );
}

function SystemSection() {
  const sys = useCore((s) => s.sysinfo);
  const running = useCore((s) => s.computer.running);
  const cpuPct = useCpuLoad(sys);

  const memTotalKb = Number(sys?.mem?.total_kb) || 0;
  const memUsedKb = Number(sys?.mem?.used_kb) || 0;
  const memAvailKb = Math.max(0, memTotalKb - memUsedKb);
  const diskTotal = Number(sys?.disk?.total) || 0;
  const diskUsed = Number(sys?.disk?.used) || 0;
  const diskFree = Number(sys?.disk?.free) || 0;
  const cores = Number(sys?.cpu?.cores) || 0;

  return (
    <div className="set-pane">
      <header className="set-pane__head">
        <h3>System</h3>
        <p>
          Read live from the remote Codespace — not from this device.
          <span className="set-live">
            <i className={running ? "set-live__dot" : "set-live__dot set-live__dot--off"} />
            {running ? "connected" : "computer offline"}
          </span>
        </p>
      </header>

      <div className="set-grid">
        <Meter
          icon={<Database size={14} />}
          label="Memory"
          used={memUsedKb}
          total={memTotalKb}
          usedLabel={memTotalKb ? fmtBytes(memUsedKb * 1024) : "—"}
          freeLabel={memTotalKb ? fmtBytes(memAvailKb * 1024) : "—"}
        />
        <Meter
          icon={<HardDrive size={14} />}
          label="Storage"
          used={diskUsed}
          total={diskTotal}
          usedLabel={diskUsed ? fmtBytes(diskUsed) : "—"}
          freeLabel={diskFree ? fmtBytes(diskFree) : "—"}
        />
      </div>

      <div className="set-card">
        <div className="set-card__title">
          <Cpu size={14} /> Processor
        </div>
        <Row label="Logical cores" value={cores ? `${cores} ${cores === 1 ? "core" : "cores"}` : "—"} />
        <Row label="Current load" value={cpuPct === null ? <em className="set-dim">sampling…</em> : `${cpuPct}%`} />
        {sys?.loadavg?.length ? (
          <Row label="Load average" value={sys.loadavg.map((n) => n.toFixed(2)).join("   ·   ")} mono />
        ) : null}
      </div>

      <div className="set-card">
        <div className="set-card__title">
          <Monitor size={14} /> Machine
        </div>
        <Row label="Operating system" value={sys?.os || "—"} />
        {sys?.kernel ? <Row label="Kernel" value={sys.kernel} mono /> : null}
        <Row label="Hostname" value={sys?.hostname || "—"} mono />
        <Row
          label="Display"
          value={sys?.display?.size ? `${sys.display.size.replace("x", " × ")} px` : "—"}
          mono
        />
        <Row label="Uptime" value={sys?.uptime ? fmtUptime(sys.uptime) : "—"} />
      </div>

      <div className="set-card">
        <div className="set-card__title">
          <Activity size={14} /> Browser
        </div>
        <Row
          label="Chrome"
          value={
            sys?.browser?.running ? (
              <span className="set-ok">
                <Check size={12} /> running
              </span>
            ) : (
              <span className="set-dim">not running</span>
            )
          }
        />
        {sys?.browser?.pid ? <Row label="Process" value={`pid ${sys.browser.pid}`} mono /> : null}
        {sys?.browser?.start_url ? <Row label="Start page" value={sys.browser.start_url} mono /> : null}
        <p className="set-note">
          <Zap size={12} /> The live screen is the real framebuffer. These figures describe the machine behind
          it.
        </p>
      </div>
    </div>
  );
}

function PersonalizationSection({
  wallpaper,
  onWallpaper,
}: {
  wallpaper: string;
  onWallpaper: (id: string) => void;
}) {
  const active = wallpaper;

  return (
    <div className="set-pane">
      <header className="set-pane__head">
        <h3>Personalization</h3>
        <p>Wallpaper only. It is the desktop’s background and never touches the remote screen.</p>
      </header>

      <div className="set-wallgrid">
        {WALLPAPERS.map((w) => {
          const on = w.id === active;
          return (
            <button
              key={w.id}
              className={`set-wall${on ? " set-wall--on" : ""}`}
              onClick={() => {
                if (on) return;
                haptic("light");
                onWallpaper(w.id);
              }}
              aria-pressed={on}
              title={w.name}
            >
              <span className="set-wall__thumb" style={{ background: previewCss(w) }}>
                {on ? (
                  <span className="set-wall__check">
                    <Check size={13} />
                  </span>
                ) : null}
              </span>
              <span className="set-wall__name">{w.name}</span>
            </button>
          );
        })}
      </div>

      <p className="set-note">
        <Palette size={12} /> Every option is drawn with gradients instead of an image file, so the previews
        are exact and switching one costs no download.
      </p>
    </div>
  );
}

export function SettingsApp({
  wallpaper,
  onWallpaper,
}: {
  wallpaper: string;
  onWallpaper: (id: string) => void;
}) {
  const [section, setSection] = useState<SectionId>("system");

  return (
    <div className="settings-app">
      <nav className="settings-app__nav" aria-label="Settings sections">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            className={`settings-app__tab${section === s.id ? " settings-app__tab--on" : ""}`}
            onClick={() => {
              haptic("light");
              setSection(s.id);
            }}
            aria-current={section === s.id}
          >
            <s.icon size={15} />
            {s.label}
          </button>
        ))}
      </nav>
      <div className="settings-app__body">
        {section === "system" ? (
          <SystemSection />
        ) : (
          <PersonalizationSection wallpaper={wallpaper} onWallpaper={onWallpaper} />
        )}
      </div>
    </div>
  );
}
