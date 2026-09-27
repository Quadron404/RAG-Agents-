import type { BotDef, Run } from "../store";

export type AgentStatus = "online" | "thinking" | "working" | "waiting" | "completed" | "offline";

export interface AgentState {
  status: AgentStatus;
  label: string;
}

function stepBots(run: Run | null): { bot: "researcher" | "engineer" | "navigator"; active: boolean }[] {
  const out: { bot: "researcher" | "engineer" | "navigator"; active: boolean }[] = [
    { bot: "researcher", active: false },
    { bot: "engineer", active: false },
    { bot: "navigator", active: false },
  ];
  if (!run) return out;
  for (const step of run.steps) {
    if (step.status !== "working") continue;
    const tools = step.tools.map((t) => t.tool);
    const isResearch = tools.some((t) => t === "web_search" || t === "fetch_url");
    const isBrowser = step.role === "browser" || tools.some((t) => t === "browser" || t === "browser_screenshot");
    if (isResearch) out.find((x) => x.bot === "researcher")!.active = true;
    else if (isBrowser) out.find((x) => x.bot === "navigator")!.active = true;
    else out.find((x) => x.bot === "engineer")!.active = true;
  }
  return out;
}

export function useAgentStates(run: Run | null): Record<BotDef["id"], AgentState> {
  const states: Record<BotDef["id"], AgentState> = {
    commander: { status: "online", label: "ready" },
    researcher: { status: "online", label: "ready" },
    engineer: { status: "online", label: "ready" },
    navigator: { status: "online", label: "ready" },
  };

  if (!run) return states;

  const actives = stepBots(run);
  for (const a of actives) {
    if (a.active) {
      states[a.bot] = { status: "working", label: "working" };
    }
  }

  const phase = run.phase;
  const busy = phase === "working";
  const thinking = phase === "planning" || phase === "synthesizing";

  if (thinking) states.commander = { status: "thinking", label: phase === "synthesizing" ? "synthesizing" : "planning" };
  else if (busy) states.commander = { status: "working", label: "orchestrating" };
  else if (phase === "done") states.commander = { status: "completed", label: "finished" };

  return states;
}

export function runSummary(run: Run | null): { dot: "online" | "thinking" | "working" | "waiting"; label: string } {
  if (!run || run.phase === "done") return { dot: "online", label: "online · workforce ready" };
  if (run.phase === "planning") return { dot: "thinking", label: "Commander is planning…" };
  if (run.phase === "working") {
    const active = run.steps.find((s) => s.status === "working");
    const doneCount = run.steps.filter((s) => s.status === "done").length;
    if (active) {
      return {
        dot: "working",
        label: `${active.title} · ${doneCount}/${run.steps.length} done`,
      };
    }
    return { dot: "working", label: "workers running…" };
  }
  return { dot: "thinking", label: "Commander is writing the answer…" };
}