import { describe, expect, it } from "vitest";
import { describeSchedule, nextOccurrence, validateSchedule } from "../src/recurrence";
import type { RoutineSchedule } from "../src/routines-types";

function next(schedule: RoutineSchedule, timeZone: string, fromIso: string): number | null {
  return nextOccurrence(schedule, timeZone, Date.parse(fromIso));
}

function iso(ms: number | null): string | null {
  return ms === null ? null : new Date(ms).toISOString();
}

describe("validateSchedule", () => {
  it("normalises a daily time", () => {
    const result = validateSchedule("daily", { time: "9:00" }, "UTC");
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.schedule).toEqual({ type: "daily", time: "09:00" });
  });

  it("rejects an unknown timezone as ambiguous", () => {
    const result = validateSchedule("daily", { time: "09:00" }, "Mars/Olympus");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.ambiguous).toBe(true);
  });

  it("flags a weekly routine with no weekday as ambiguous", () => {
    const result = validateSchedule("weekly", { time: "09:00", weekdays: [] }, "UTC");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.ambiguous).toBe(true);
  });

  it("de-duplicates and sorts weekdays", () => {
    const result = validateSchedule("weekly", { time: "08:00", weekdays: [5, 1, 5, 1] }, "UTC");
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.schedule).toEqual({ type: "weekly", time: "08:00", weekdays: [1, 5] });
  });

  it("rejects a monthly day out of range", () => {
    const result = validateSchedule("monthly", { time: "09:00", day: 32 }, "UTC");
    expect(result.ok).toBe(false);
  });
});

describe("nextOccurrence", () => {
  it("returns the next daily wall time in the routine's timezone", () => {
    const schedule: RoutineSchedule = { type: "daily", time: "09:00" };
    expect(iso(next(schedule, "America/New_York", "2024-03-09T20:00:00Z"))).toBe(
      "2024-03-10T13:00:00.000Z",
    );
  });

  it("shifts a wall time skipped by the spring-forward gap", () => {
    // 02:30 does not exist on 2024-03-10 in New York; the first instant at or
    // after it (03:30 local) is the deterministic answer.
    const schedule: RoutineSchedule = { type: "daily", time: "02:30" };
    expect(iso(next(schedule, "America/New_York", "2024-03-09T20:00:00Z"))).toBe(
      "2024-03-10T07:30:00.000Z",
    );
  });

  it("fires a repeated fall-back wall time once, at the earlier instant", () => {
    const schedule: RoutineSchedule = { type: "daily", time: "01:30" };
    expect(iso(next(schedule, "America/New_York", "2024-11-03T00:00:00Z"))).toBe(
      "2024-11-03T05:30:00.000Z",
    );
  });

  it("finds the next matching weekday", () => {
    const schedule: RoutineSchedule = { type: "weekly", time: "09:00", weekdays: [1] };
    // 2024-03-06 is a Wednesday; the next Monday is 2024-03-11.
    expect(iso(next(schedule, "UTC", "2024-03-06T00:00:00Z"))).toBe("2024-03-11T09:00:00.000Z");
  });

  it("clamps a monthly day to the last day of a short month", () => {
    const schedule: RoutineSchedule = { type: "monthly", time: "09:00", day: 31 };
    expect(iso(next(schedule, "UTC", "2024-01-31T09:00:00Z"))).toBe("2024-02-29T09:00:00.000Z");
  });

  it("returns null for a one-time routine in the past", () => {
    const schedule: RoutineSchedule = { type: "once", at: "2020-01-01T00:00:00.000Z" };
    expect(next(schedule, "UTC", "2024-01-01T00:00:00.000Z")).toBeNull();
  });

  it("is strict about 'after', so a just-fired occurrence is not reselected", () => {
    const schedule: RoutineSchedule = { type: "once", at: "2024-01-01T00:00:00.000Z" };
    expect(next(schedule, "UTC", "2024-01-01T00:00:00.000Z")).toBeNull();
  });
});

describe("describeSchedule", () => {
  it("describes a weekly schedule in words", () => {
    expect(describeSchedule({ type: "weekly", time: "09:00", weekdays: [1, 3] }, "Europe/Paris")).toBe(
      "every Mon, Wed at 09:00 (Europe/Paris)",
    );
  });
});
