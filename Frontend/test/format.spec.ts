import { describe, expect, it } from "vitest";
import { relativeTime, timeStr, parseAgentField } from "../src/lib/format";

describe("format utilities", () => {
  it("relativeTime formats timestamps", () => {
    const now = Date.now();
    expect(relativeTime(now)).toBeTruthy();
    expect(typeof relativeTime(now)).toBe("string");
  });

  it("timeStr formats timestamps", () => {
    expect(timeStr()).toBeTruthy();
    expect(timeStr(0)).toBeTruthy();
  });

  it("parseAgentField handles different agent formats", () => {
    expect(parseAgentField("commander").kind).toBe("commander");
    expect(parseAgentField("chief").kind).toBe("commander");
    expect(parseAgentField("worker:engineer").kind).toBe("worker");
    expect(parseAgentField("worker:browser").kind).toBe("worker");
    expect(parseAgentField("user").kind).toBe("user");
  });
});
