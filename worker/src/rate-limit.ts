/** Per-user rate limiting for chat message endpoints. */
import type { Env } from "./env";

interface Window {
  count: number;
  resetAt: number;
}

const windows = new Map<string, Window>();

function now(): number {
  return Date.now();
}

/** How many chat messages a user may send per window. */
const MAX_PER_WINDOW = 60;

/** Window length in milliseconds. */
const WINDOW_MS = 60_000;

/** Record one request for a user and return whether it is allowed. */
export function allowChatRequest(userId: string): { allowed: boolean; remaining: number; resetAt: number } {
  const t = now();
  let w = windows.get(userId);
  if (!w || t > w.resetAt) {
    w = { count: 1, resetAt: t + WINDOW_MS };
    windows.set(userId, w);
    return { allowed: true, remaining: MAX_PER_WINDOW - 1, resetAt: w.resetAt };
  }
  w.count += 1;
  const allowed = w.count <= MAX_PER_WINDOW;
  return { allowed, remaining: Math.max(0, MAX_PER_WINDOW - w.count), resetAt: w.resetAt };
}
