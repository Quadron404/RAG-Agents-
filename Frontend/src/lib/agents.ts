import type { BotDef, Run, Step } from "../store";

export type AgentStatus = "online" | "thinking" | "working" | "waiting" | "completed" | "offline";

/**
 * What a running tool is actually doing, in the user's terms.
 *
 * These labels are derived from the tool call the executor reported, never
 * invented ahead of it: a step only says "Searching the web" because a
 * ``web_search`` call is in flight right now.  The point is that the person
 * watching sees the real activity rather than a generic spinner.
 */
const TOOL_ACTIVITY: Record<string, string> = {
  web_search: "Searching the web",
  fetch_url: "Reading a page",
  browse: "Browsing",
  browser: "Browsing",
  browser_screenshot: "Looking at the screen",
  shell: "Running a command",
  read_file: "Reading a file",
  write_file: "Writing a file",
  list_dir: "Listing files",
};

/** The live label for the whole run, from its phase and in-flight tools. */
export function activityLabel(run: Run | null): string {
  if (!run || run.phase === "done" || run.phase === "idle") return "";
  if (run.phase === "planning") return "Thinking · planning the approach";
  if (run.phase === "synthesizing") return "Preparing the final answer";
  const working = run.steps.filter((s) => s.status === "working");
  const runningTool = working.flatMap((s) => s.tools).find((t) => t.status === "running");
  if (runningTool && TOOL_ACTIVITY[runningTool.tool]) return TOOL_ACTIVITY[runningTool.tool];
  if (working.length > 1) return `Working · ${working.length} specialists at once`;
  return "Working";
}

/** The live label for one step, from the tool it currently has in flight. */
export function stepActivity(step: Step): string {
  const running = step.tools.find((t) => t.status === "running");
  if (running && TOOL_ACTIVITY[running.tool]) return TOOL_ACTIVITY[running.tool];
  return "working";
}

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