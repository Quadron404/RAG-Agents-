export function timeStr(ts?: number): string {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function relativeTime(ts?: number): string {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 45) return "now";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}m`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h`;
  if (diff < 604800) return `${Math.round(diff / 86400)}d`;
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function dayLabel(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((startOf(today) - startOf(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}

export function fmtBytes(n: unknown): string {
  const v = Number(n) || 0;
  if (v < 1024) return `${Math.round(v)} B`;
  if (v < 1048576) return `${(v / 1024).toFixed(1)} KB`;
  if (v < 1073741824) return `${(v / 1048576).toFixed(1)} MB`;
  return `${(v / 1073741824).toFixed(2)} GB`;
}

export function fmtUptime(s: unknown): string {
  const v = Number(s) || 0;
  if (v < 60) return `${Math.round(v)}s`;
  if (v < 3600) return `${Math.round(v / 60)} min`;
  if (v < 86400) return `${(v / 3600).toFixed(1)} h`;
  return `${(v / 86400).toFixed(1)} d`;
}

export function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

export function truncateMid(text: string, max = 46): string {
  if (text.length <= max) return text;
  const half = Math.floor((max - 3) / 2);
  return text.slice(0, half) + "…" + text.slice(text.length - half);
}

export function parseAgentField(agent: string): {
  kind: "commander" | "user" | "worker";
  role: string;
  title: string;
} {
  if (agent === "user" || agent === "") return { kind: "user", role: "", title: "" };
  if (agent === "commander") return { kind: "commander", role: "commander", title: "Commander" };
  const parts = agent.split(":");
  const role = parts[1] || "worker";
  const title = parts.slice(2).join(":") || "Worker";
  return { kind: "worker", role, title };
}

export function escAttr(s: unknown): string {
  return String(s ?? "").replace(/"/g, "&quot;").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}