import { useEffect } from "react";
import { usePrefs, useUi } from "./store";
import { useCore } from "./core";
import { setHapticsEnabled } from "./lib/haptics";
import { Rail, TabBar } from "./components/Shell";
import { TopBar } from "./components/TopBar";
import { ContextMenuHost, ActionSheetHost, ToastHost } from "./components/ui";
import { ChatView, ChatListPane } from "./views/Chat";
import { BotsView } from "./views/Bots";
import { TasksView } from "./views/Tasks";
import { ComputerView } from "./views/Computer";
import { FilesView } from "./views/Files";
import { SettingsSheet } from "./views/Settings";

export function App() {
  const view = useUi((s) => s.view);
  const convOpen = useUi((s) => s.convOpen);
  const connect = useCore((s) => s.connect);
  const haptics = usePrefs((s) => s.haptics);

  useEffect(() => {
    setHapticsEnabled(haptics);
  }, [haptics]);

  // The agent session is the app's control channel.  It is opened on mount
  // because there is no longer a login gate in front of it.
  useEffect(() => {
    connect();
  }, [connect]);

  return (
    <div className={`app${view === "chat" && convOpen ? " app--list-open" : ""}`}>
      <div className="aurora" aria-hidden />
      <Rail />
      {view === "chat" ? <ChatListPane /> : null}
      <div className="main">
        {view === "chat" ? null : <TopBar />}
        {view === "chat" ? <ChatView /> : null}
        {view === "bots" ? <BotsView /> : null}
        {view === "tasks" ? <TasksView /> : null}
        {view === "computer" ? <ComputerView /> : null}
        {view === "files" ? <FilesView /> : null}
      </div>
      <TabBar />
      <SettingsSheet />
      <ToastHost />
      <ContextMenuHost />
      <ActionSheetHost />
    </div>
  );
}