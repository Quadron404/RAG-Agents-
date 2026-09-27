import { useState } from "react";
import { Cloud, ExternalLink } from "lucide-react";
import { usePrefs, useUi, type Appearance } from "../store";
import { haptic, hapticSupported } from "../lib/haptics";
import { Switch, Sheet } from "../components/ui";

const APPEARANCES: { id: Appearance; name: string; tone: "light" | "dark" | "auto" }[] = [
  { id: "light", name: "Light", tone: "light" },
  { id: "dark", name: "Dark", tone: "dark" },
  { id: "system", name: "System", tone: "auto" },
];

export function SettingsView() {
  const appearance = usePrefs((s) => s.appearance);
  const hapticsPref = usePrefs((s) => s.haptics);
  const compact = usePrefs((s) => s.compact);
  const setAppearance = usePrefs((s) => s.setAppearance);
  const setHaptics = usePrefs((s) => s.setHaptics);
  const setCompact = usePrefs((s) => s.setCompact);
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
              <div className="setrow__sub mono">ws://localhost:8000/ws/me</div>
            </div>
            <ExternalLink size={17} style={{ color: "var(--text-3)" }} />
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
  const [confirm, setConfirm] = useState(false);

  const reset = () => {
    localStorage.removeItem("rag.prefs.v1");
    localStorage.removeItem("rag.ignores.v1");
    localStorage.removeItem("rag.pins");
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
        <div className="setrow__sub">Preferences, pins and archives are cleared.</div>
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