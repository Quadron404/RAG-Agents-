import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Cpu,
  Database,
  Folder,
  HardDrive,
  Loader2,
  Monitor,
  MonitorSmartphone,
  Power,
  RefreshCw,
  Terminal,
} from "lucide-react";
import { useCore } from "../core";
import { fmtBytes, fmtUptime } from "../lib/format";
import { haptic } from "../lib/haptics";
import { VncScreen } from "../components/VncScreen";
import { FilesPane } from "./Files";

type CompTab = "screen" | "browser" | "files" | "terminal";

export function ComputerView() {
  const computer = useCore((s) => s.computer);
  const sysinfo = useCore((s) => s.sysinfo);
  const screen = useCore((s) => s.screen);
  const webTitle = useCore((s) => s.webTitle);
  const webUrl = useCore((s) => s.webUrl);
  const computerStart = useCore((s) => s.computerStart);
  const computerStop = useCore((s) => s.computerStop);
  const requestScreen = useCore((s) => s.requestScreen);
  const callTool = useCore((s) => s.callTool);
  const [tab, setTab] = useState<CompTab>("screen");
  const [navUrl, setNavUrl] = useState("");
  const [navBusy, setNavBusy] = useState(false);
  const [shotBusy, setShotBusy] = useState(false);

  const running = computer.running;
  const booting = computer.booting;

  // Report the memory the machine actually has, not the guest's slightly
  // smaller MemTotal: "4 GB" is the real figure, and 3.8 GB reads like
  // something is missing. Usage stays available on hover.
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

  const navigate = useCallback(
    async (url: string) => {
      const target = url.trim();
      if (!target) return;
      setNavBusy(true);
      setNavUrl(target);
      haptic("medium");
      const r = await callTool("browser", { action: "goto", url: target });
      setNavBusy(false);
      if (r.error) {
        useCore.getState().setWeb("page unavailable", target);
      } else if (r.title || r.url) {
        useCore.getState().setWeb(r.title, r.url);
      }
    },
    [callTool]
  );

  const refreshShot = useCallback(async () => {
    if (!running) return;
    setShotBusy(true);
    requestScreen();
    setTimeout(() => setShotBusy(false), 700);
  }, [running, requestScreen]);

  const gotoPrev = () => callTool("browser", { action: "back" });
  const gotoNext = () => callTool("browser", { action: "forward" });

  return (
    <div className="computer">
      <div className="comp__statusbar">
        <div className="comp__tabs">
          <button className={`comp__tab${tab === "screen" ? " comp__tab--on" : ""}`} onClick={() => setTab("screen")}>
            <MonitorSmartphone size={15} /> Live Screen
          </button>
          <button className={`comp__tab${tab === "browser" ? " comp__tab--on" : ""}`} onClick={() => setTab("browser")}>
            <Monitor size={15} /> Browser
          </button>
          <button className={`comp__tab${tab === "files" ? " comp__tab--on" : ""}`} onClick={() => setTab("files")}>
            <Folder size={15} /> Files
          </button>
          <button className={`comp__tab${tab === "terminal" ? " comp__tab--on" : ""}`} onClick={() => setTab("terminal")}>
            <Terminal size={15} /> Terminal
          </button>
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
          <Power size={18} />
        </button>
      </div>

      {!running && !booting ? (
        <div className="comp__bootcard">
          <div className="comp__bootbox">
            <div className="comp__bootorb">
              <Monitor size={38} />
            </div>
            <div className="comp__bootbody">The workforce computer is offline</div>
            <div className="comp__bootsub">
              Bring up the remote computer so the Engineer, Navigator and their tools
              get a real machine to work on — browser, files and terminal.
            </div>
            <button className="btn btn--primary btn--lg" onClick={() => (haptic("heavy"), computerStart())}>
              <Power size={17} /> Boot the computer
            </button>
          </div>
        </div>
      ) : booting ? (
        <div className="comp__bootcard">
          <div className="comp__bootbox">
            <div className="comp__bootorb" style={{ animation: "pulse 1.2s ease infinite" }}>
              <Loader2 size={38} style={{ animation: "spin 1s linear infinite" }} />
            </div>
            <div className="comp__bootbody">Booting your computer…</div>
            <div className="comp__bootsub">Starting the display, browser and terminal.</div>
            <div className="boot-progress">
              <i style={{ width: "62%" }} />
            </div>
          </div>
        </div>
      ) : (
        <div className="comp__main">
          {tab === "screen" ? <VncScreen running={running} /> : null}

          {tab === "browser" ? (
            <>
              <div className="comp__addrbar">
                <button className="act-btn" onClick={gotoPrev} title="Back" aria-label="Back">
                  <ArrowLeft size={16} />
                </button>
                <button className="act-btn" onClick={gotoNext} title="Forward" aria-label="Forward">
                  <ArrowRight size={16} />
                </button>
                <input
                  value={navUrl}
                  onChange={(e) => setNavUrl(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), navigate(navUrl))}
                  placeholder="Address or search…"
                  aria-label="Address bar"
                />
                <button className="act-btn" onClick={() => navigate(navUrl)} disabled={navBusy} title="Go" aria-label="Go">
                  {navBusy ? <Loader2 size={16} style={{ animation: "spin 1s linear infinite" }} /> : <ArrowRight size={16} />}
                </button>
                <button className="act-btn" onClick={refreshShot} disabled={shotBusy} title="Refresh screen" aria-label="Refresh screen">
                  <RefreshCw size={16} style={shotBusy ? { animation: "spin 1s linear infinite" } : undefined} />
                </button>
              </div>
              <div className="scmap-wrap">
                <div className="cdp-note">
                  Agent-driven browser view (CDP). The authoritative picture is the Live Screen tab.
                </div>
                {screen ? (
                  <>
                    <div className="scmap">
                      <img className="frame" src={screen} alt="Agent-driven view of the remote browser" />
                      <div className="browser-label">
                        {webTitle || "your browser"} — {webUrl || "agent run"}
                      </div>
                    </div>
                  </>
                ) : (
                  <div className="scmap-wrap" style={{ color: "var(--text-2)", fontSize: 13, gap: 10 }}>
                    <Loader2 size={20} style={{ animation: "spin 1s linear infinite" }} />
                    Waiting for a live frame…
                  </div>
                )}
              </div>
            </>
          ) : null}

          {tab === "files" ? <FilesPane embedded /> : null}
          {tab === "terminal" ? <TerminalPane /> : null}
        </div>
      )}
    </div>
  );
}

function TerminalPane() {
  const terminal = useCore((s) => s.terminal);
  const shell = useCore((s) => s.shell);
  const vmPath = useCore((s) => s.vmPath);
  const clearTerminal = useCore((s) => s.clearTerminal);
  const [cmd, setCmd] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [terminal]);

  const run = () => {
    if (!cmd.trim()) return;
    shell(cmd);
    setCmd("");
  };

  return (
    <div className="vmterm" style={{ flex: 1, maxHeight: "none", height: "100%" }}>
      <div className="vmterm__head">
        <Terminal size={13} />
        <span style={{ flex: 1 }}>terminal · {vmPath}</span>
        <button className="act-btn" onClick={clearTerminal} title="Clear terminal" aria-label="Clear terminal">
          <RefreshCw size={13} />
        </button>
      </div>
      <div className="vmterm__body">
        {terminal.map((l, i) => (
          <div key={i} className={`tline ${l.kind === "cmd" ? "tcmd" : l.kind === "err" ? "terr" : "tout"}`}>
            {l.kind === "cmd" ? (
              <span className="tline">
                <span className="tp">➜</span> <span className="tpath">{vmPath}</span> {l.text}
              </span>
            ) : (
              l.text
            )}
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <div className="vmterm__inputbar">
        <span className="prompt">➜ {vmPath}</span>
        <input
          value={cmd}
          onChange={(e) => setCmd(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), run())}
          placeholder="run a command…"
          aria-label="Terminal command"
          autoFocus
        />
      </div>
    </div>
  );
}