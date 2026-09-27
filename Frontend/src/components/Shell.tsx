import { Bot, Folder, ListTodo, MessageSquare, Monitor, Settings, type LucideIcon } from "lucide-react";
import { useUi, type ViewId } from "../store";
import { useCore } from "../core";

const NAV: { id: ViewId; label: string; icon: LucideIcon }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "bots", label: "Bots", icon: Bot },
  { id: "tasks", label: "Tasks", icon: ListTodo },
  { id: "computer", label: "Computer", icon: Monitor },
  { id: "files", label: "Files", icon: Folder },
];

/* ---- Slim icon rail (desktop) ---------------------------------------------- */

export function Rail() {
  const view = useUi((s) => s.view);
  const setView = useUi((s) => s.setView);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);
  const computerRunning = useCore((s) => s.computer.running);
  const status = useCore((s) => s.status);
  const run = useCore((s) => s.run);
  const activeThreadId = useCore((s) => s.activeThreadId);

  const running = !!run && run.phase !== "done" && run.phase !== "idle" && run.threadId === activeThreadId;

  return (
    <aside className="rail" aria-label="Primary">
      <div className="rail__brand" aria-hidden>
        <svg className="brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="7" cy="17" r="2.4" />
          <circle cx="17" cy="7" r="2.4" />
          <path d="M9.4 15.6L14.6 8.4" />
        </svg>
      </div>

      <nav className="rail__nav">
        {NAV.map((n) => {
          const Icon = n.icon;
          const live = (n.id === "chat" && running) || (n.id === "computer" && computerRunning);
          return (
            <button
              key={n.id}
              className={`rail__item${view === n.id ? " rail__item--on" : ""}`}
              onClick={() => setView(n.id)}
              aria-label={n.label}
              title={n.label}
            >
              <Icon size={20} strokeWidth={1.9} />
              {live ? <span className="rail__live" /> : null}
            </button>
          );
        })}
      </nav>

      <div className="rail__foot">
        <span
          className={`rail__conn ${status === "online" ? "rail__conn--on" : status === "connecting" ? "rail__conn--busy" : ""}`}
          title={status === "online" ? "connected" : status === "connecting" ? "connecting…" : "reconnecting…"}
        />
        <button className="rail__item" onClick={() => setSettingsOpen(true)} aria-label="Settings" title="Settings">
          <Settings size={20} strokeWidth={1.9} />
        </button>
      </div>
    </aside>
  );
}

/* ---- Mobile tab bar ------------------------------------------------------- */

export function TabBar() {
  const view = useUi((s) => s.view);
  const setView = useUi((s) => s.setView);
  const computer = useCore((s) => s.computer);
  const run = useCore((s) => s.run);

  const running = run && run.phase !== "done" && run.phase !== "idle";
  return (
    <nav className="tabbar" aria-label="Sections">
      {NAV.map((n) => {
        const Icon = n.icon;
        const live = n.id === "computer" && computer.running;
        const busy = n.id === "chat" && !!running;
        return (
          <button
            key={n.id}
            className={`tabbar__item${view === n.id ? " tabbar__item--on" : ""}`}
            onClick={() => setView(n.id)}
            aria-current={view === n.id ? "page" : undefined}
          >
            <Icon size={21} strokeWidth={1.8} />
            <span>{n.label}</span>
            {live || busy ? <span className="tabbar__dot" /> : null}
          </button>
        );
      })}
    </nav>
  );
}