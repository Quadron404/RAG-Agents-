import { Check, Hourglass, Loader2, Terminal, X } from "lucide-react";
import { botForRole } from "../store";
import { useCore } from "../core";
import { fmtDuration } from "../lib/format";

const COLUMNS = [
  { id: "queued", label: "In queue", icon: Hourglass, color: "var(--text-3)" },
  { id: "working", label: "Running now", icon: Loader2, color: "var(--accent)" },
  { id: "done", label: "Completed", icon: Check, color: "var(--green)" },
] as const;

export function TasksView() {
  const run = useCore((s) => s.run);
  const steps = run?.steps || [];
  const busy = run && run.phase !== "done" && run.phase !== "idle";

  return (
    <div className="tasks">
      <div className="tasks__inner">
        <div className="tasks__head">
          <div>
            <div className="tasks__title">Tasks</div>
            <div className="tasks__sub">
              {steps.length
                ? `${steps.filter((s) => s.status === "done").length} of ${steps.length} steps completed`
                : "A plan appears here the moment the Commander assigns it."}
            </div>
          </div>
          {busy ? <span className="dot dot--working" /> : null}
        </div>

        {!steps.length ? (
          <div
            className="t-card"
            style={{ padding: 20, color: "var(--text-2)", fontSize: 13.5 }}
          >
            No tasks yet. Ask anything in Chat and the workforce will break it
            into steps right here — live.
          </div>
        ) : (
          <div className="board">
            {COLUMNS.map((col) => {
              const Icon = col.icon;
              const list = steps.filter((s) => s.status === col.id);
              return (
                <div className="col" key={col.id}>
                  <div className="col__head">
                    <Icon size={15} style={{ color: col.color }} />
                    <span>{col.label}</span>
                    <span className="col__count">{list.length}</span>
                  </div>
                  <div className="col__body">
                    {list.map((s) => {
                      const bot = botForRole(s.role);
                      const pct = s.status === "done" ? 100 : s.status === "working" ? 55 : 8;
                      const failed = s.tools.some((t) => t.status === "error");
                      return (
                        <div className={`t-card${s.status === "done" ? " t-card--done" : ""}`} key={s.id}>
                          <div className="t-card__top">
                            <span className="t-card__art" style={{ background: bot.gradient }}>
                              <span
                                className="dot"
                                style={{
                                  position: "absolute",
                                  right: -2,
                                  bottom: -2,
                                  width: 8,
                                  height: 8,
                                  border: "2px solid var(--bg2)",
                                  boxSizing: "content-box",
                                  background:
                                    failed ? "var(--red)" : s.status === "done" ? "var(--green)" : s.status === "working" ? "var(--amber)" : "var(--text-3)",
                                }}
                              />
                              {bot.glyph}
                            </span>
                            <div style={{ minWidth: 0 }}>
                              <div className="t-card__title">{s.title}</div>
                              <div className="t-card__meta">
                                {bot.name} ·{" "}
                                {s.status === "done"
                                  ? "completed"
                                  : s.status === "working"
                                    ? "working"
                                    : "queued"}
                                {failed ? " · some tools failed" : ""}
                              </div>
                            </div>
                            {failed ? (
                              <X size={14} style={{ color: "var(--red)", marginLeft: "auto", flex: "0 0 auto" }} />
                            ) : null}
                          </div>
                          <div className="t-card__bar">
                            <i className={s.status === "working" ? "work" : ""} style={{ width: `${pct}%` }} />
                          </div>
                          {s.tools.length ? (
                            <div className="t-card__tools">
                              <Terminal size={13} />
                              {s.tools.filter((t) => t.status === "running").length
                                ? `${s.tools.filter((t) => t.status === "running").length} running · `
                                : ""}
                              {s.tools.length} tool calls
                            </div>
                          ) : null}
                        </div>
                      );
                    })}
                    {!list.length ? (
                      <div style={{ padding: "10px 4px", fontSize: 12, color: "var(--text-3)" }}>
                        {col.id === "working" ? "Nothing running" : col.id === "queued" ? "Queue is clear" : "Nothing completed"}
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {run && run.startedAt ? (
          <div style={{ marginTop: 18, fontSize: 12, color: "var(--text-3)" }}>
            Run started {fmtDuration(Date.now() - run.startedAt)} ago
          </div>
        ) : null}
      </div>
    </div>
  );
}