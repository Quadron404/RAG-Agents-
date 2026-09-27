import { useEffect, useRef, useState } from "react";
import { RefreshCw, Terminal } from "lucide-react";

import { useCore } from "../core";

/**
 * A real shell on the remote machine.
 *
 * This is the same component the Computer page has always used, lifted out of
 * views/Computer.tsx so the desktop window can mount it without the view having
 * to know how windows work.  It drives the backend's own shell through the agent
 * socket -- there is no simulated output here.
 */
export function TerminalPane() {
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
    <div className="vmterm vmterm--window">
      <div className="vmterm__body">
        {terminal.length === 0 ? (
          <div className="vmterm__empty">
            <Terminal size={22} />
            <span>Type a command to run it on the remote machine.</span>
          </div>
        ) : null}
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
        <button className="act-btn" onClick={clearTerminal} title="Clear terminal" aria-label="Clear terminal">
          <RefreshCw size={13} />
        </button>
      </div>
    </div>
  );
}
