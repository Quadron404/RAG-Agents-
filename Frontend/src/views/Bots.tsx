import { BOTS, useUi } from "../store";
import { useCore } from "../core";
import { useAgentStates } from "../lib/agents";
import { haptic } from "../lib/haptics";

export function BotsView() {
  const run = useCore((s) => s.run);
  const activeThreadId = useCore((s) => s.activeThreadId);
  const states = useAgentStates(run && run.threadId === activeThreadId ? run : null);
  const setBot = useUi((s) => s.setBot);
  const setView = useUi((s) => s.setView);
  const toast = useUi((s) => s.toast);

  return (
    <div className="bots">
      <div className="bots__inner">
        <div className="bots__head">
          <div className="bots__title">Your workforce</div>
          <div className="bots__sub">
            Every request is planned by the Commander, then assigned to the
            specialist best suited for each step — together they finish in one,
            calm conversation.
          </div>
        </div>
        <div className="bots__grid">
          {BOTS.map((b) => {
            const st = states[b.id];
            return (
              <button
                key={b.id}
                className={`bot-card${b.id === "commander" ? " bot-card--commander" : ""}`}
                onClick={() => {
                  haptic("medium");
                  setBot(b.id);
                  setView("chat");
                  toast(`Talking to ${b.name}`, "info");
                }}
              >
                <div className="bot-card__art" style={{ background: b.gradient }}>
                  {b.glyph}
                  <span className={`bot-card__status bot-card__status--${st.status}`} />
                </div>
                <div className="bot-card__name">{b.name}</div>
                <div className="bot-card__role">{b.role}</div>
                <div className="bot-card__blurb">{b.blurb}</div>
                <div className="bot-card__tags">
                  {b.tags.map((t) => (
                    <span key={t} className="pill">
                      {t}
                    </span>
                  ))}
                </div>
                <div className="bot-card__foot">
                  <span className={`dot dot--${st.status}`} />
                  <span style={{ fontSize: 12.5, color: "var(--text-3)", fontWeight: 600 }}>{st.label}</span>
                  <span className="bot-card__goal" style={{ marginLeft: "auto", fontSize: 12.5, color: b.color, fontWeight: 650 }}>
                    {b.goal}
                  </span>
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}