import { useState } from "react";
import { Image, Search, Sparkles } from "lucide-react";

const ARTIFACTS = [
  {
    id: "a-1",
    title: "Q3 budget model",
    kind: "spreadsheet",
    desc: "Built from your expense exports. 12-month forecast with variance vs plan.",
    updated: "2h ago",
    preview: "📊",
  },
  {
    id: "a-2",
    title: "Landing page copy",
    kind: "document",
    desc: "Hero section, features, pricing table. Tone: bold, developer-first.",
    updated: "5h ago",
    preview: "📄",
  },
  {
    id: "a-3",
    title: "On-call runbook",
    kind: "document",
    desc: "Incident response steps, escalation contacts, rollback procedure.",
    updated: "1d ago",
    preview: "📘",
  },
];

export function MakeView() {
  const [q, setQ] = useState("");
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");

  const items = ARTIFACTS.filter((a) =>
    a.title.toLowerCase().includes(q.toLowerCase()) || a.desc.toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <div className="make">
      <div className="make__head">
        <div>
          <div className="make__title">Make</div>
          <div className="make__sub">
            Everything Chief has built for you — open, edit, or ask for changes.
          </div>
        </div>
        <div className="make__controls">
          <div className="search">
            <Search size={15} />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search artifacts…"
              aria-label="Search artifacts"
            />
          </div>
          <div className="seg" role="tablist" aria-label="View">
            <button
              className={`seg__item${viewMode === "grid" ? " seg__item--on" : ""}`}
              onClick={() => setViewMode("grid")}
              aria-label="Grid view"
            >
              <Image size={15} />
            </button>
            <button
              className={`seg__item${viewMode === "list" ? " seg__item--on" : ""}`}
              onClick={() => setViewMode("list")}
              aria-label="List view"
            >
              <Image size={15} />
            </button>
          </div>
          <button className="btn btn--primary" onClick={() => {}} aria-label="New artifact">
            <Sparkles size={16} />
            New artifact
          </button>
        </div>
      </div>

      <div className={viewMode === "grid" ? "make__grid" : "make__list"}>
        {items.length ? (
          items.map((a) => (
            <div key={a.id} className={`m-card m-card--${a.kind}`}>
              <div className="m-card__top">
                <span className="m-card__kind">{a.preview}</span>
                <div className="m-card__meta">
                  <span className="m-card__updated">{a.updated}</span>
                </div>
              </div>
              <div className="m-card__body">
                <div className="m-card__title">{a.title}</div>
                <div className="m-card__desc">{a.desc}</div>
              </div>
              <div className="m-card__foot">
                <span className="m-chip">{a.kind}</span>
                <span className="m-chip">editable</span>
              </div>
            </div>
          ))
        ) : (
          <div className="m-empty">
            <Sparkles size={32} style={{ opacity: 0.5 }} />
            <p style={{ color: "var(--text-2)", marginTop: 10 }}>No artifacts yet.</p>
            <p style={{ color: "var(--text-3)", fontSize: 12, marginTop: 4 }}>
              Ask Chief in Chat to build something — a document, a dashboard, a
              model — and it will appear here.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
