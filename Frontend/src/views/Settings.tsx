import { useState } from "react";
import { Cloud, ExternalLink, Mail } from "lucide-react";
import { usePrefs, useUi, type Appearance } from "../store";
import { useAuth, type GmailStatus } from "../auth";
import { useCore } from "../core";
import { haptic, hapticSupported } from "../lib/haptics";
import { Switch, Sheet } from "../components/ui";

const APPEARANCES: { id: Appearance; name: string; tone: "light" | "dark" | "auto" }[] = [
  { id: "light", name: "Light", tone: "light" },
  { id: "dark", name: "Dark", tone: "dark" },
  { id: "system", name: "System", tone: "auto" },
];

type MailboxTone = "green" | "amber" | "red" | "";

/** The label and tone for a mailbox state, worded so it never overstates. */
function mailboxState(gmail: GmailStatus): { label: string; tone: MailboxTone; sub: string } {
  switch (gmail.status) {
    case "connected":
      return { label: "Connected", tone: "green", sub: gmail.email || "Your Gmail account is connected." };
    case "revoked":
    case "reauth_required":
      return { label: "Reconnect", tone: "amber", sub: "Google revoked access. Reconnect to keep the mailbox available." };
    case "error":
      return { label: "Needs attention", tone: "amber", sub: "We couldn't refresh access just now. Reconnect if it persists." };
    case "disconnected":
      return { label: "Not connected", tone: "", sub: "Connect Gmail so agents can work with your mailbox." };
    default:
      return { label: "Unknown", tone: "", sub: "Mailbox status is unavailable right now." };
  }
}

/** Connect / reconnect / disconnect, reflecting only the real backend state. */
function MailboxRow() {
  const gmail = useAuth((s) => s.gmail);
  const connectGmail = useAuth((s) => s.connectGmail);
  const disconnectGmail = useAuth((s) => s.disconnectGmail);
  const [busy, setBusy] = useState(false);
  const state = mailboxState(gmail);
  const connected = gmail.connected;

  return (
    <div className="setrow">
      <div className="about-logo">
        <Mail size={24} />
      </div>
      <div className="setrow__meta">
        <div className="setrow__label">
          Gmail {gmail.email ? <span className="mono">· {gmail.email}</span> : null}
        </div>
        <div className="setrow__sub">{state.sub}</div>
      </div>
      <span className={`pill${state.tone ? ` pill--${state.tone}` : ""}`}>{state.label}</span>
      {connected ? (
        <button
          className="btn btn--ghost"
          disabled={busy}
          onClick={async () => {
            haptic("medium");
            setBusy(true);
            await disconnectGmail();
            setBusy(false);
          }}
        >
          Disconnect
        </button>
      ) : (
        <button
          className="btn"
          onClick={() => {
            haptic("medium");
            connectGmail();
          }}
        >
          Connect
        </button>
      )}
    </div>
  );
}

export function SettingsView() {
  const appearance = usePrefs((s) => s.appearance);
  const hapticsPref = usePrefs((s) => s.haptics);
  const compact = usePrefs((s) => s.compact);
  const setAppearance = usePrefs((s) => s.setAppearance);
  const setHaptics = usePrefs((s) => s.setHaptics);
  const setCompact = usePrefs((s) => s.setCompact);
  const developerMode = useAuth((s) => s.user?.developer_mode === 1);
  const setDeveloperMode = useAuth((s) => s.setDeveloperMode);
  const toast = useUi((s) => s.toast);
  const supportsHaptics = hapticSupported();

  return (
    <div className="settings">
      <div className="settings__inner">
        <div className="settings__title">Settings</div>

        <div className="setgroup">
          <div className="setgroup__head">Appearance</div>
          <div className="appearance-row">
            {APPEARANCES.map((a) => (
              <button
                key={a.id}
                className={`appearance-opt${appearance === a.id ? " appearance-opt--on" : ""}`}
                onClick={() => {
                  haptic("medium");
                  setAppearance(a.id);
                }}
              >
                <span className={`thumbnail thumbnail--${a.tone}`}>
                  <i />
                  <b />
                </span>
                <span className="appearance-opt__name">{a.name}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="setgroup">
          <div className="setgroup__head">Feedback</div>
          <div className="setrow">
            <div className="setrow__meta">
              <div className="setrow__label">Haptics</div>
              <div className="setrow__sub">
                {supportsHaptics
                  ? "Subtle vibrations on actions, messages and toggles."
                  : "This device doesn't expose a vibration API — the option is kept for devices that do."}
              </div>
            </div>
            <Switch on={hapticsPref} onChange={setHaptics} label="Haptics" />
          </div>
          <div className="setrow">
            <div className="setrow__meta">
              <div className="setrow__label">Compact messages</div>
              <div className="setrow__sub">Tighter spacing inside conversations.</div>
            </div>
            <Switch on={compact} onChange={setCompact} label="Compact messages" />
          </div>
        </div>

        <div className="setgroup">
          <div className="setgroup__head">Your workspace</div>
          <div className="setrow">
            <div className="setrow__meta">
              <div className="setrow__label">Agent identity link</div>
              <div className="setrow__sub">
                The workforce never leaves your machine. Conversations, tools and
                the computer stay under your control.
              </div>
            </div>
            <span className="pill pill--green">Local</span>
          </div>
          <div className="setrow" style={{ cursor: "pointer" }} onClick={() => toast("Copied workspace link", "ok")}>
            <div className="setrow__meta">
              <div className="setrow__label">Backend endpoint</div>
              <div className="setrow__sub mono">same origin · /ws</div>
            </div>
            <ExternalLink size={17} style={{ color: "var(--text-3)" }} />
          </div>
        </div>

        <div className="setgroup">
          <div className="setgroup__head">Mailbox</div>
          <MailboxRow />
        </div>

        <div className="setgroup">
          <div className="setgroup__head">Developer Mode</div>
          <div className="setrow">
            <div className="setrow__meta">
              <div className="setrow__label">Developer Mode</div>
              <div className="setrow__sub">
                Manual history generation; no automatic daily or 30-day summaries.
              </div>
            </div>
            <Switch
              on={developerMode}
              onChange={setDeveloperMode}
              label={developerMode ? "On" : "Off"}
            />
          </div>
        </div>

        <div className="setgroup">
          <div className="setgroup__head">About</div>
          <div className="setrow">
            <div className="about-logo">
              <Cloud size={24} />
            </div>
            <div className="setrow__meta">
              <div className="setrow__label">RAG Agents</div>
              <div className="setrow__sub">
                Your AI workforce — Commander, Researcher, Engineer and
                Navigator working together from one conversation.
              </div>
            </div>
            <span className="pill">1.0</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function SettingsSheet() {
  const open = useUi((s) => s.settingsOpen);
  const close = useUi((s) => s.setSettingsOpen);
  const appearance = usePrefs((s) => s.appearance);
  const hapticsPref = usePrefs((s) => s.haptics);
  const compact = usePrefs((s) => s.compact);
  const setAppearance = usePrefs((s) => s.setAppearance);
  const setHaptics = usePrefs((s) => s.setHaptics);
  const setCompact = usePrefs((s) => s.setCompact);
  const developerMode = useAuth((s) => s.user?.developer_mode === 1);
  const setDeveloperMode = useAuth((s) => s.setDeveloperMode);
  const [confirm, setConfirm] = useState(false);
  const user = useAuth((s) => s.user);
  const logout = useAuth((s) => s.logout);

  const reset = async () => {
    const ok = await useCore.getState().resetAccount();
    if (!ok) return;
    localStorage.removeItem("rag.prefs.v1");
    localStorage.removeItem("rag.ignores.v1");
    localStorage.removeItem("rag.pins");
    localStorage.removeItem("rag.active-thread.v1");
    window.dispatchEvent(new StorageEvent("storage"));
    location.reload();
  };

  return (
    <Sheet
      open={open}
      onClose={() => close(false)}
      title="Settings"
      placement="side"
    >
      <div className="appearance-row">
        {APPEARANCES.map((a) => (
          <button
            key={a.id}
            className={`appearance-opt${appearance === a.id ? " appearance-opt--on" : ""}`}
            onClick={() => {
              haptic("medium");
              setAppearance(a.id);
            }}
          >
            <span className={`thumbnail thumbnail--${a.tone}`}>
              <i />
              <b />
            </span>
            <span className="appearance-opt__name">{a.name}</span>
          </button>
        ))}
      </div>
      <div className="setrow">
        <div className="setrow__meta">
          <div className="setrow__label">Appearance</div>
          <div className="setrow__sub">System follows your OS theme.</div>
        </div>
      </div>
      <div className="setrow">
        <div className="setrow__meta">
          <div className="setrow__label">Haptics</div>
          <div className="setrow__sub">Vibration feedback on actions.</div>
        </div>
        <Switch on={hapticsPref} onChange={setHaptics} label="Haptics" />
      </div>
      <div className="setrow">
        <div className="setrow__meta">
          <div className="setrow__label">Compact messages</div>
          <div className="setrow__sub">Tighter conversation spacing.</div>
        </div>
        <Switch on={compact} onChange={setCompact} label="Compact messages" />
      </div>

      <SheetDivider label="Account" />
      <div className="setrow">
        <div className="setrow__meta">
          <div className="setrow__label">{user?.name || user?.email || "Signed in"}</div>
          <div className="setrow__sub">{user?.email || "Your Google session is active."}</div>
        </div>
        <button
          className="btn btn--ghost"
          onClick={() => {
            haptic("medium");
            logout();
          }}
        >
          Sign out
        </button>
      </div>

      <SheetDivider label="Mailbox" />
      <MailboxRow />

      <SheetDivider label="Developer Mode" />
      <div className="setrow">
        <div className="setrow__meta">
          <div className="setrow__label">Developer Mode</div>
          <div className="setrow__sub">
            Manual history generation; no automatic daily or 30-day summaries.
          </div>
        </div>
        <Switch
          on={developerMode}
          onChange={setDeveloperMode}
          label={developerMode ? "On" : "Off"}
        />
      </div>

      <SheetDivider label="Danger zone" />
      <ConfirmRow
        confirm={confirm}
        setConfirm={setConfirm}
        onReset={() => {
          setConfirm(false);
          haptic("medium");
          reset();
        }}
      />
    </Sheet>
  );
}

function SheetDivider({ label }: { label: string }) {
  return (
    <div style={{ fontSize: 11, fontWeight: 800, letterSpacing: "0.09em", textTransform: "uppercase", color: "var(--text-3)", padding: "16px 18px 6px" }}>
      {label}
    </div>
  );
}

function ConfirmRow({
  confirm,
  setConfirm,
  onReset,
}: {
  confirm: boolean;
  setConfirm: (v: boolean) => void;
  onReset: () => void;
}) {
  return confirm ? (
    <div className="setrow">
      <div className="setrow__meta">
        <div className="setrow__label" style={{ color: "var(--red)" }}>
          Reset everything?
        </div>
        <div className="setrow__sub">Deletes all your chats, history and indexed documents from the server.</div>
      </div>
      <button className="btn btn--danger" onClick={onReset}>
        Reset
      </button>
      <button className="btn btn--ghost" onClick={() => setConfirm(false)}>
        Cancel
      </button>
    </div>
  ) : (
    <div className="setrow">
      <div className="setrow__meta">
        <div className="setrow__label">Reset local state</div>
        <div className="setrow__sub">Clears appearance, pins and archives.</div>
      </div>
      <button className="btn btn--ghost" onClick={() => setConfirm(true)}>
        Reset
      </button>
    </div>
  );
}