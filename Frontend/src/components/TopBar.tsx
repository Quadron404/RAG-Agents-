import { Search, Settings } from "lucide-react";
import { useUi, type ViewId } from "../store";
import { useCore } from "../core";
import { runSummary } from "../lib/agents";
import { IconButton } from "./ui";

const TITLES: Record<ViewId, { title: () => string; sub: () => string }> = {
  chat: {
    title: () => {
      const { threads, activeThreadId } = useCore.getState();
      return threads.find((t) => t.thread_id === activeThreadId)?.title || "New conversation";
    },
    sub: () => {
      const s = runSummaryForActive();
      return s ? `${s.label} · → ${useUi.getState().bot}` : `talking to ${useUi.getState().bot}`;
    },
  },
  bots: {
    title: () => "Your workforce",
    sub: () => runSummaryForActive()?.label || "Four specialists, one Commander",
  },
  tasks: {
    title: () => "Tasks",
    sub: () => {
      const run = useCore.getState().run;
      if (!run || run.phase === "done") return "Nothing running";
      return `${run.steps.filter((s) => s.status === "done").length}/${run.steps.length} steps done`;
    },
  },
  computer: {
    title: () => "Computer",
    sub: () => {
      const computer = useCore.getState().computer;
      return computer.running ? "online · remote computer" : computer.booting ? "booting…" : "offline";
    },
  },
  files: {
    title: () => "Files",
    sub: () => useCore.getState().vmPath || "your workspace",
  },
};

function runSummaryForActive() {
  const { run, activeThreadId } = useCore.getState();
  return run && run.threadId === activeThreadId && run.phase !== "done" && run.phase !== "idle"
    ? runSummary(run)
    : null;
}

export function TopBar() {
  const view = useUi((s) => s.view);
  const searchActive = useUi((s) => s.searchActive);
  const setSearchActive = useUi((s) => s.setSearchActive);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);

  const meta = TITLES[view];

  return (
    <header className="topbar">
      <div className="topbar__left">
        <div className="topbar__title">{meta.title()}</div>
        <div className="topbar__sub">
          <span className="dot dot--online" />
          {meta.sub()}
        </div>
      </div>
      <div className="topbar__actions">
        {view === "chat" ? (
          <IconButton
            icon={Search}
            active={searchActive}
            onClick={() => setSearchActive(!searchActive)}
            title="Search in thread"
            ariaLabel="Search in thread"
          />
        ) : null}
        <IconButton icon={Settings} onClick={() => setSettingsOpen(true)} title="Settings" ariaLabel="Settings" />
      </div>
    </header>
  );
}