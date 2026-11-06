import { describe, it, expect } from "vitest";
import type { Run, Step } from "../store";
import { activityLabel, stepActivity, runSummary } from "./agents";

function run(phase: Run["phase"], steps: Step[] = []): Run {
  return {
    threadId: "t",
    phase,
    plan: null,
    steps,
    commanderDelta: "",
    startedAt: Date.now(),
    cancel: false,
  };
}

function step(title: string, status: Step["status"] = "done", tools: Step["tools"] = []): Step {
  return {
    id: title,
    agent: title,
    title,
    role: "worker",
    status,
    tools,
    delta: "",
    report: "",
  };
}

describe("agents.activityLabel", () => {
  it("is empty when idle or done", () => {
    expect(activityLabel(run("idle"))).toBe("");
    expect(activityLabel(run("done"))).toBe("");
    expect(activityLabel(null)).toBe("");
  });
  it("reports planning and synthesizing", () => {
    expect(activityLabel(run("planning"))).toBe("Thinking · planning the approach");
    expect(activityLabel(run("synthesizing"))).toBe("Preparing the final answer");
  });
  it("reports the running tool's activity", () => {
    const r = run("working", [step("s", "working", [{ id: "t1", tool: "web_search", args: {}, status: "running", output: "", error: "", open: false }])]);
    expect(activityLabel(r)).toBe("Searching the web");
  });
  it("counts concurrent specialists", () => {
    const r = run("working", [
      step("a", "working"),
      step("b", "working"),
    ]);
    expect(activityLabel(r)).toBe("Working · 2 specialists at once");
  });
});

describe("agents.stepActivity", () => {
  it("derives activity from the running tool", () => {
    const s = step("s", "working", [{ id: "t1", tool: "shell", args: {}, status: "running", output: "", error: "", open: false }]);
    expect(stepActivity(s)).toBe("Running a command");
  });
  it("falls back to working", () => {
    expect(stepActivity(step("s", "working"))).toBe("working");
  });
});

describe("agents.runSummary", () => {
  it("is online when no run", () => {
    expect(runSummary(null)).toEqual({ dot: "online", label: "online · workforce ready" });
  });
  it("reports planning", () => {
    expect(runSummary(run("planning"))).toEqual({ dot: "thinking", label: "Commander is planning…" });
  });
  it("reports current step progress", () => {
    const r = run("working", [
      step("a", "done"),
      step("b", "working", [{ id: "t1", tool: "shell", args: {}, status: "running", output: "", error: "", open: false }]),
    ]);
    expect(runSummary(r)).toEqual({ dot: "working", label: "b · 1/2 done" });
  });
});
