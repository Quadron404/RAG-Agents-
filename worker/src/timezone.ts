/**
 * Timezone handling for the Worker.
 *
 * workerd ships full ICU/Intl data, so IANA names like ``America/New_York``
 * resolve natively -- no ``tzdata`` package is needed here (unlike the Python
 * backend).  All calendar math is done in the owner's local time so daily
 * summaries align with the day the owner actually experienced, including
 * 23-hour spring-forward days and 25-hour fall-back days.
 */
export type LocalDay = { startMs: number; endMs: number; date: string };

const FALLBACK_TZ = "UTC";

/** Validate an IANA timezone name, degrading to ``UTC`` for anything unknown. */
export function validateTimezone(value: unknown): string {
  if (value == null) return FALLBACK_TZ;
  const name = String(value).trim();
  if (!name) return FALLBACK_TZ;
  try {
    // ``Intl.DateTimeFormat`` throws RangeError for an unknown time zone.
    new Intl.DateTimeFormat("en-US", { timeZone: name });
    return name;
  } catch {
    return FALLBACK_TZ;
  }
}

/** ``YYYY-MM-DD`` for ``whenMs`` expressed in ``tzName``. */
export function localDateKey(tzName: string, whenMs: number): string {
  const tz = validateTimezone(tzName);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(whenMs));
}

/** Hour/minute/second/millisecond of ``whenMs`` measured from local midnight. */
function timeOfDayMs(tz: string, whenMs: number): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).formatToParts(new Date(whenMs));
  const get = (name: string) => {
    const p = parts.find((x) => x.type === name);
    return p ? Number(p.value) : 0;
  };
  const hour = get("hour");
  const minute = get("minute");
  const second = get("second");
  const ms = get("fractionalSecond");
  return ((hour * 60 + minute) * 60 + second) * 1000 + ms;
}

/** Epoch ms of local midnight (00:00:00.000) on the day containing ``whenMs``. */
function startOfLocalDay(tz: string, whenMs: number): number {
  return whenMs - timeOfDayMs(tz, whenMs);
}

/**
 * ``(startMs, endMs, date)`` for the local calendar day containing ``whenMs``.
 *
 * ``endMs`` is local midnight of the *next* day.  Because a day can be 23 or 25
 * hours across a DST transition, the end is found by locating tomorrow's local
 * midnight rather than adding a fixed 24 hours.
 */
export function localDay(tzName: string, whenMs: number): LocalDay {
  const tz = validateTimezone(tzName);
  const date = localDateKey(tz, whenMs);
  const startMs = startOfLocalDay(tz, whenMs);
  // Tomorrow's local midnight.  Adding 25h always lands at or after tomorrow
  // midnight (a day is at most 25h), so taking the start of that local day is
  // exactly the end of today, across spring-forward and fall-back transitions.
  const endMs = startOfLocalDay(tz, whenMs + 25 * 60 * 60 * 1000);
  return { startMs, endMs, date };
}

/** Yesterday's local date in ``tzName`` relative to ``whenMs``. */
export function previousLocalDate(tzName: string, whenMs: number): string {
  const tz = validateTimezone(tzName);
  const { startMs } = localDay(tz, whenMs);
  // The previous local day's midnight is at most 25h before today's midnight.
  const prevStartMs = startOfLocalDay(tz, startMs - 25 * 60 * 60 * 1000);
  return localDateKey(tz, prevStartMs);
}
