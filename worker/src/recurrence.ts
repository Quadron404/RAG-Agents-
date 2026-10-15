/**
 * Deterministic, timezone-aware recurrence for routines.
 *
 * Everything here is pure: given a schedule, an IANA timezone and an instant,
 * it returns the next instant (epoch ms, UTC) or null.  No clock is read, so a
 * scheduler test can ask "what is the next run after this moment?" and get the
 * same answer the deployed Worker would give.
 *
 * Rules that are deliberately fixed, because an ambiguous schedule must still
 * resolve to exactly one instant:
 *
 *   * Wall-clock times are interpreted in the routine's timezone, not the
 *     server's.  "09:00 daily" means 09:00 where the user is.
 *   * A wall time skipped by a spring-forward DST gap resolves to the first
 *     instant at or after it (so 02:30 on a gap day becomes 03:00).
 *   * A wall time repeated by a fall-back DST overlap resolves to the *first*
 *     (earlier-offset) occurrence, so a "daily at 01:30" fires once, not twice.
 *   * Monthly on a day the month does not have (the 31st in February) clamps to
 *     the last day of that month, rather than skipping the month silently.
 *
 * A schedule that cannot be represented this way is rejected by
 * ``validateSchedule`` with a reason, which is what lets the agent ask the user
 * to disambiguate instead of guessing.
 */
import type { RoutineSchedule, ScheduleType } from "./routines-types";

export interface ScheduleOk {
  ok: true;
  schedule: RoutineSchedule;
  timeZone: string;
}
export interface ScheduleErr {
  ok: false;
  error: string;
  /** True when the schedule is well-formed but genuinely ambiguous. */
  ambiguous: boolean;
}
export type ScheduleResult = ScheduleOk | ScheduleErr;

//: Accepts "9:00" as well as "09:00" -- an agent filling the field from natural
//: language should not have to zero-pad before validateSchedule will take it.
const TIME_RE = /^([0-9]|0[0-9]|1[0-9]|2[0-3]):([0-5][0-9])$/;
const WEEK_DAYS = 7;

function err(error: string, ambiguous = false): ScheduleErr {
  return { ok: false, error, ambiguous };
}

/** True when the string names a timezone the runtime can actually resolve. */
export function isValidTimeZone(timeZone: string): boolean {
  if (!timeZone) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });
    return true;
  } catch {
    return false;
  }
}

interface ZonedParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

function partsInZone(ms: number, timeZone: string): ZonedParts {
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const map: Record<string, string> = {};
  for (const part of dtf.formatToParts(new Date(ms))) {
    if (part.type !== "literal") map[part.type] = part.value;
  }
  return {
    year: Number(map.year),
    month: Number(map.month),
    day: Number(map.day),
    hour: Number(map.hour),
    minute: Number(map.minute),
    second: Number(map.second),
  };
}

/** The zone's offset (local - UTC) in milliseconds at a given instant. */
function offsetAt(ms: number, timeZone: string): number {
  const aligned = ms - (ms % 1000);
  const p = partsInZone(aligned, timeZone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return asUtc - aligned;
}

/**
 * A wall-clock time in a zone, as an instant.
 *
 * Two candidate offsets are computed -- the one in force where the wall time
 * would land, and the one in force at the naive instant -- and the candidates
 * they produce are resolved by the DST rules:
 *
 *   * a normal time: both candidates are the same instant;
 *   * a fall-back overlap: both are valid, so the *earlier* one is chosen;
 *   * a spring-forward gap: neither matches, so the first instant at or after
 *     the requested wall time (the larger candidate) is chosen.
 */
function wallToUtc(
  year: number,
  month: number,
  day: number,
  hour: number,
  minute: number,
  timeZone: string,
): number {
  const asUtc = Date.UTC(year, month - 1, day, hour, minute, 0, 0);
  const o0 = offsetAt(asUtc, timeZone);
  const c0 = asUtc - o0;
  const o1 = offsetAt(c0, timeZone);
  const c1 = asUtc - o1;

  const m0 = matches(partsInZone(c0, timeZone), year, month, day, hour, minute);
  const m1 = c1 !== c0 && matches(partsInZone(c1, timeZone), year, month, day, hour, minute);

  if (m0 && m1) return Math.min(c0, c1); // fall-back overlap: the earlier run
  if (m0) return c0;
  if (m1) return c1;
  return Math.max(c0, c1); // gap: the first instant after the skipped wall time
}

function matches(p: ZonedParts, y: number, mo: number, d: number, h: number, mi: number): boolean {
  return p.year === y && p.month === mo && p.day === d && p.hour === h && p.minute === mi;
}

function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

/** Weekday (0=Sunday..6=Saturday) of a calendar date, timezone-independent. */
function weekdayOf(year: number, month: number, day: number): number {
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
}

function parseTime(value: unknown): { hour: number; minute: number; text: string } | null {
  if (typeof value !== "string") return null;
  const match = TIME_RE.exec(value.trim());
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  return { hour, minute, text: `${String(hour).padStart(2, "0")}:${match[2]}` };
}

/**
 * Turn the tool's raw fields into a normalised schedule, or explain why not.
 *
 * ``ambiguous`` is reserved for schedules that are valid but under-specified
 * (e.g. a weekly routine with no weekday), so the caller can tell "that is not a
 * schedule" from "which day did you mean?".
 */
export function validateSchedule(
  scheduleType: unknown,
  definition: unknown,
  timeZone: unknown,
): ScheduleResult {
  const type = String(scheduleType ?? "").trim() as ScheduleType;
  const tz = String(timeZone ?? "").trim() || "UTC";
  if (!isValidTimeZone(tz)) {
    return err(`unknown timezone ${JSON.stringify(tz)}`, true);
  }
  const def = (definition && typeof definition === "object" ? definition : {}) as Record<string, unknown>;

  if (type === "once") {
    const at = def.at;
    if (typeof at !== "string" || !at.trim()) {
      return err("a one-time routine needs an 'at' timestamp");
    }
    const ms = Date.parse(at.trim());
    if (!Number.isFinite(ms)) {
      return err(`could not read ${JSON.stringify(String(at))} as a timestamp`);
    }
    return { ok: true, schedule: { type: "once", at: new Date(ms).toISOString() }, timeZone: tz };
  }

  if (type === "daily") {
    const time = parseTime(def.time);
    if (!time) return err("a daily routine needs a time like '09:00'");
    return { ok: true, schedule: { type: "daily", time: time.text }, timeZone: tz };
  }

  if (type === "weekly") {
    const time = parseTime(def.time);
    if (!time) return err("a weekly routine needs a time like '09:00'");
    const raw = Array.isArray(def.weekdays) ? def.weekdays : [];
    const weekdays = Array.from(
      new Set(raw.map((value) => Number(value)).filter((value) => Number.isInteger(value) && value >= 0 && value < WEEK_DAYS)),
    ).sort((a, b) => a - b);
    if (weekdays.length === 0) {
      return err("a weekly routine needs at least one weekday (0=Sunday..6=Saturday)", true);
    }
    return { ok: true, schedule: { type: "weekly", time: time.text, weekdays }, timeZone: tz };
  }

  if (type === "monthly") {
    const time = parseTime(def.time);
    if (!time) return err("a monthly routine needs a time like '09:00'");
    const day = Number(def.day);
    if (!Number.isInteger(day) || day < 1 || day > 31) {
      return err("a monthly routine needs a day of month between 1 and 31");
    }
    return { ok: true, schedule: { type: "monthly", time: time.text, day }, timeZone: tz };
  }

  return err(`unsupported schedule type ${JSON.stringify(String(scheduleType ?? ""))}`);
}

//: A hard upper bound on the search, so a schedule that can never come true
//: returns null instead of looping.  Four years comfortably covers the longest
//: gap any real recurrence can have (a clumsy monthly day plus a leap year).
const MAX_DAYS = 366 * 4;

/**
 * The next instant strictly after ``fromMs``, or null for a spent one-time
 * routine.  Strictly-after is what makes the scheduler idempotent: a run that
 * just fired cannot be selected again by asking for "the next one now".
 */
export function nextOccurrence(
  schedule: RoutineSchedule,
  timeZone: string,
  fromMs: number,
): number | null {
  if (schedule.type === "once") {
    const at = Date.parse(schedule.at);
    return Number.isFinite(at) && at > fromMs ? at : null;
  }

  const time = parseTime(schedule.time);
  if (!time) return null;
  const start = partsInZone(fromMs, timeZone);

  if (schedule.type === "daily") {
    for (let i = 0; i <= MAX_DAYS; i++) {
      const date = addDays(start.year, start.month, start.day, i);
      const candidate = wallToUtc(date.year, date.month, date.day, time.hour, time.minute, timeZone);
      if (candidate > fromMs) return candidate;
    }
    return null;
  }

  if (schedule.type === "weekly") {
    const wanted = new Set(schedule.weekdays);
    for (let i = 0; i <= MAX_DAYS; i++) {
      const date = addDays(start.year, start.month, start.day, i);
      if (!wanted.has(weekdayOf(date.year, date.month, date.day))) continue;
      const candidate = wallToUtc(date.year, date.month, date.day, time.hour, time.minute, timeZone);
      if (candidate > fromMs) return candidate;
    }
    return null;
  }

  // monthly: walk month by month and clamp to the last valid day.
  for (let i = 0; i < 48; i++) {
    const year = start.year + Math.floor((start.month - 1 + i) / 12);
    const month = ((start.month - 1 + i) % 12) + 1;
    const day = Math.min(schedule.day, daysInMonth(year, month));
    const candidate = wallToUtc(year, month, day, time.hour, time.minute, timeZone);
    if (candidate > fromMs) return candidate;
  }
  return null;
}

function addDays(year: number, month: number, day: number, delta: number): { year: number; month: number; day: number } {
  const d = new Date(Date.UTC(year, month - 1, day + delta));
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
}

/** A short human sentence for the routine's description and tool output. */
export function describeSchedule(schedule: RoutineSchedule, timeZone: string): string {
  switch (schedule.type) {
    case "once":
      return `once at ${schedule.at} (UTC)`;
    case "daily":
      return `every day at ${schedule.time} (${timeZone})`;
    case "weekly": {
      const names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
      return `every ${schedule.weekdays.map((d) => names[d]).join(", ")} at ${schedule.time} (${timeZone})`;
    }
    case "monthly":
      return `monthly on day ${schedule.day} at ${schedule.time} (${timeZone})`;
  }
}

export function parseScheduleDefinition(raw: unknown): RoutineSchedule | null {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const value = JSON.parse(raw);
    if (value && typeof value === "object" && typeof value.type === "string") {
      return value as RoutineSchedule;
    }
  } catch {
    // fall through
  }
  return null;
}
