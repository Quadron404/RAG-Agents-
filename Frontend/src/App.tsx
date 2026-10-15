import { useEffect } from "react";
import type { CSSProperties } from "react";
import { usePrefs, useUi } from "./store";
import { useCore } from "./core";
import { useAuth, loginHref } from "./auth";
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

const gateStyle: CSSProperties = {
  minHeight: "100dvh",
  display: "grid",
  placeItems: "center",
  padding: 24,
  textAlign: "center",
};

function Splash() {
  return (
    <div style={gateStyle}>
      <div style={{ opacity: 0.7, font: "14px/1.6 ui-monospace, Menlo, Consolas, monospace" }}>
        Connecting…
      </div>
    </div>
  );
}

function SignIn() {
  return (
    <div style={gateStyle}>
      <div style={{ maxWidth: 360 }}>
        <div style={{ fontSize: 20, fontWeight: 700, marginBottom: 8 }}>RAG Agents</div>
        <p style={{ opacity: 0.75, margin: "0 0 20px" }}>
          Sign in with Google to reach your workforce and your private computer.
        </p>
        <a
          href={loginHref()}
          style={{
            display: "inline-block",
            padding: "10px 18px",
            borderRadius: 10,
            border: "1px solid rgba(127,127,127,.35)",
            textDecoration: "none",
            color: "inherit",
            fontWeight: 600,
          }}
        >
          Continue with Google
        </a>
      </div>
    </div>
  );
}

export function App() {
  const view = useUi((s) => s.view);
  const convOpen = useUi((s) => s.convOpen);
  const connect = useCore((s) => s.connect);
  const haptics = usePrefs((s) => s.haptics);

  const ready = useAuth((s) => s.ready);
  const authenticated = useAuth((s) => s.authenticated);
  const bootstrap = useAuth((s) => s.bootstrap);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  // The Gmail consent flow returns to "/" with a short outcome flag.  Report it
  // once, strip it from the URL so a refresh does not repeat the toast, and
  // re-read the mailbox state because consent may have just changed it.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const gmail = params.get("gmail");
    const auth = params.get("auth");
    if (!gmail && !auth) return;
    const messages: Record<string, [string, "ok" | "err" | "info"]> = {
      connected: ["Gmail connected.", "ok"],
      denied: ["Gmail access was not granted.", "info"],
      wrong_account: ["That Google account is not the one you signed in with.", "err"],
      no_refresh_token: ["Google returned no refresh token — connect again.", "err"],
      "auth:denied": ["Sign-in was cancelled.", "info"],
    };
    const key = auth ? `auth:${auth}` : `gmail:${gmail}`;
    const entry = messages[key];
    if (entry) useUi.getState().toast(entry[0], entry[1]);
    params.delete("gmail");
    params.delete("auth");
    const query = params.toString();
    history.replaceState(null, "", location.pathname + (query ? `?${query}` : ""));
    useAuth.getState().refreshGmail();
  }, []);

  useEffect(() => {
    setHapticsEnabled(haptics);
  }, [haptics]);

  // The agent session is the app's control channel.  It is opened only once the
  // edge session is known good: an anonymous socket would be rejected anyway,
  // and connecting before the CSRF token is loaded would fail every mutation.
  useEffect(() => {
    if (authenticated) connect();
  }, [authenticated, connect]);

  if (!ready) return <Splash />;
  if (!authenticated) return <SignIn />;

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