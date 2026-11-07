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

  it("accepts an interval with a positive millisecond value", () => {
    const result = validateSchedule("interval", { every_ms: 5 * 60 * 60 * 1000 }, "UTC");
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.schedule).toEqual({ type: "interval", everyMs: 5 * 60 * 60 * 1000 });
  });

  it("accepts alternative interval field names", () => {
    const result = validateSchedule("interval", { everyMs: 90 * 60 * 1000 }, "UTC");
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.schedule).toEqual({ type: "interval", everyMs: 90 * 60 * 1000 });
  });

  it("rejects a zero interval", () => {
    const result = validateSchedule("interval", { every_ms: 0 }, "UTC");
    expect(result.ok).toBe(false);
  });

  it("rejects a negative interval", () => {
    const result = validateSchedule("interval", { every_ms: -300_000 }, "UTC");
    expect(result.ok).toBe(false);
  });

  it("rejects an interval over 365 days", () => {
    const result = validateSchedule("interval", { every_ms: 366 * 24 * 60 * 60 * 1000 }, "UTC");
    expect(result.ok).toBe(false);
  });

  it("rejects a non-integer interval", () => {
    const result = validateSchedule("interval", { every_ms: 1000.5 }, "UTC");
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

describe("nextOccurrence > interval", () => {
  it("adds the interval to the from instant in absolute milliseconds", () => {
    const schedule: RoutineSchedule = { type: "interval", everyMs: 5 * 60 * 60 * 1000 }; // 5 hours
    const from = Date.parse("2024-03-10T08:00:00Z");
    expect(iso(next(schedule, "UTC", "2024-03-10T08:00:00Z"))).toBe(
      new Date(from + 5 * 60 * 60 * 1000).toISOString(),
    );
  });

  it("is DST-insensitive: absolute milliseconds, not wall-clock hours", () => {
    // A 24-hour interval from just before a DST spring-forward adds exactly
    // 24 hours of wall time, not 23 or 25.
    const schedule: RoutineSchedule = { type: "interval", everyMs: 24 * 60 * 60 * 1000 };
    const from = Date.parse("2024-03-09T06:00:00Z"); // just before NY springs forward
    expect(iso(next(schedule, "America/New_York", "2024-03-09T06:00:00Z"))).toBe(
      new Date(from + 24 * 60 * 60 * 1000).toISOString(),
    );
  });

  it("strictly after: interval from the fire instant gives the next fire, not itself", () => {
    const schedule: RoutineSchedule = { type: "interval", everyMs: 60 * 60 * 1000 }; // 1 hour
    const from = Date.parse("2024-01-01T00:00:00Z");
    expect(iso(next(schedule, "UTC", "2024-01-01T00:00:00Z"))).toBe(
      new Date(from + 60 * 60 * 1000).toISOString(),
    );
  });
});

describe("describeSchedule", () => {
  it("describes a weekly schedule in words", () => {
    expect(describeSchedule({ type: "weekly", time: "09:00", weekdays: [1, 3] }, "Europe/Paris")).toBe(
      "every Mon, Wed at 09:00 (Europe/Paris)",
    );
  });

  it("describes a 5-hour interval", () => {
    expect(describeSchedule({ type: "interval", everyMs: 5 * 60 * 60 * 1000 }, "UTC")).toBe("every 5 hours");
  });

  it("describes a 90-minute interval", () => {
    expect(describeSchedule({ type: "interval", everyMs: 90 * 60 * 1000 }, "UTC")).toBe("every 1 hour 30 minutes");
  });
});
