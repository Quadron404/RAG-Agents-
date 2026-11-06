import { describe, it, expect } from "vitest";
import {
  timeStr,
  relativeTime,
  dayLabel,
  fmtBytes,
  fmtUptime,
  fmtDuration,
  truncateMid,
  parseAgentField,
} from "./format";

describe("format.timeStr", () => {
  it("returns HH:mm for a unix timestamp", () => {
    // 2026-11-06 12:41:00 UTC — locale may render 12h or 24h; assert the HH:mm core.
    expect(timeStr(1762418460)).toMatch(/\d{2}:\d{2}/);
  });
  it("returns now when no timestamp given", () => {
    const now = timeStr();
    expect(now).toMatch(/\d{2}:\d{2}/);
  });
});

describe("format.relativeTime", () => {
  it("says now for recent events", () => {
    expect(relativeTime(Date.now() / 1000 - 10)).toBe("now");
  });
  it("uses minutes under an hour", () => {
    expect(relativeTime(Date.now() / 1000 - 45)).toBe("1m");
    expect(relativeTime(Date.now() / 1000 - 3540)).toBe("59m");
  });
  it("uses hours under a day", () => {
    expect(relativeTime(Date.now() / 1000 - 3660)).toBe("1h");
  });
  it("uses days under a week", () => {
    expect(relativeTime(Date.now() / 1000 - 200000)).toBe("2d");
  });
});

describe("format.dayLabel", () => {
  it("returns Today for now", () => {
    expect(dayLabel(Date.now() / 1000)).toBe("Today");
  });
  it("returns Yesterday for a day ago", () => {
    expect(dayLabel(Date.now() / 1000 - 90000)).toBe("Yesterday");
  });
});

describe("format.fmtBytes", () => {
  it("formats bytes, KB, MB, GB", () => {
    expect(fmtBytes(500)).toBe("500 B");
    expect(fmtBytes(1500)).toBe("1.5 KB");
    expect(fmtBytes(1500000)).toBe("1.4 MB");
    expect(fmtBytes(1500000000)).toBe("1.40 GB");
  });
});

describe("format.fmtUptime", () => {
  it("formats seconds, minutes, hours, days", () => {
    expect(fmtUptime(30)).toBe("30s");
    expect(fmtUptime(75)).toBe("1 min");
    expect(fmtUptime(3660)).toBe("1.0 h");
    expect(fmtUptime(90000)).toBe("1.0 d");
  });
});

describe("format.fmtDuration", () => {
  it("formats ms as s, m, h", () => {
    expect(fmtDuration(5000)).toBe("5s");
    expect(fmtDuration(65000)).toBe("1m");
    expect(fmtDuration(3660000)).toBe("1.0h");
  });
});

describe("format.truncateMid", () => {
  it("returns short strings unchanged", () => {
    expect(truncateMid("hello")).toBe("hello");
  });
  it("truncates long strings with ellipsis", () => {
    expect(truncateMid("abcdefghij", 10)).toBe("abcdefghij");
    expect(truncateMid("abcdefghij", 8)).toBe("ab…ij");
  });
});

describe("format.parseAgentField", () => {
  it("parses user and commander", () => {
    expect(parseAgentField("user")).toEqual({ kind: "user", role: "", title: "" });
    expect(parseAgentField("commander")).toEqual({ kind: "commander", role: "commander", title: "Chief" });
    expect(parseAgentField("chief")).toEqual({ kind: "commander", role: "commander", title: "Chief" });
  });
  it("parses worker roles", () => {
    expect(parseAgentField("worker:engineer:Build it")).toEqual({
      kind: "worker",
      role: "engineer",
      title: "Build it",
    });
    expect(parseAgentField("worker:navigator")).toEqual({
      kind: "worker",
      role: "navigator",
      title: "Worker",
    });
  });
});
