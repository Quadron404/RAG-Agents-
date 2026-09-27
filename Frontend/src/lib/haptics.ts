// Haptic feedback with graceful fallbacks for devices/platforms that do not
// support haptics. Respects the user's preference store. No-op everywhere else.

export type HapticLevel = "light" | "medium" | "heavy" | "success" | "warning" | "error";

const PATTERNS: Record<HapticLevel, number[] | null> = {
  light: [6],
  medium: [12],
  heavy: [20, 30, 12],
  success: [10, 40, 14, 40, 20],
  warning: [12, 60, 12],
  error: [30, 60, 30],
};

let enabled = true;

export function setHapticsEnabled(on: boolean) {
  enabled = on;
  if (!on && "vibrate" in navigator) {
    try {
      navigator.vibrate(0);
    } catch {
      /* noop */
    }
  }
}

export function haptic(level: HapticLevel = "light") {
  if (!enabled) return;
  if (typeof navigator === "undefined") return;
  const supported = "vibrate" in navigator;
  // iOS Safari quietly ignores vibrate — the touch feedback fallback there is
  // the CSS :active micro-interaction, which we already apply everywhere.
  if (!supported) return;
  const pattern = PATTERNS[level];
  if (!pattern) return;
  try {
    navigator.vibrate(pattern);
  } catch {
    /* noop */
  }
}

export function hapticSupported(): boolean {
  return typeof navigator !== "undefined" && "vibrate" in navigator;
}