/**
 * Wallpapers.
 *
 * Each one is a stack of CSS gradients rather than an image file.  That is not
 * laziness, it is the whole point:
 *
 *   - a wallpaper thumbnail can reuse the *exact* string, so the preview is
 *     pixel-identical to what you get instead of a lossy 160px JPEG that
 *     promises a colour it cannot deliver;
 *   - nothing to download, so switching one costs no request and no decode;
 *   - gradients are painted by the compositor and scale to any display, so a
 *     4K screen gets the same image as a laptop.
 *
 * The first entry is not a background at all.  It is `null`, meaning "leave the
 * desktop alone", which is what keeps the wallpaper that shipped with the
 * product as the default: the rule that draws it is untouched in the stylesheet
 * and simply never gets overridden.
 */

export interface Wallpaper {
  id: string;
  name: string;
  /** The CSS `background` value, or null for the built-in default. */
  css: string | null;
  /** Accent used for the selected ring and the picker label. */
  tint: string;
}

/** The default. `css: null` means the stylesheet's own rule stays in charge. */
export const DEFAULT_WALLPAPER = "default";

export const WALLPAPERS: Wallpaper[] = [
  {
    id: DEFAULT_WALLPAPER,
    name: "Aurora",
    css: null,
    tint: "#7c6aff",
  },
  {
    id: "dusk",
    name: "Dusk",
    css: [
      "radial-gradient(52% 40% at 18% 8%, rgba(255,150,113,.34), transparent 66%)",
      "radial-gradient(46% 38% at 84% 14%, rgba(226,86,140,.26), transparent 64%)",
      "radial-gradient(60% 46% at 62% 104%, rgba(120,58,178,.34), transparent 68%)",
      "linear-gradient(166deg, #2b1c3d 0%, #170f26 58%, #0d0a18 100%)",
    ].join(", "),
    tint: "#e2568c",
  },
  {
    id: "abyss",
    name: "Abyss",
    css: [
      "radial-gradient(46% 36% at 12% 6%, rgba(56,189,248,.30), transparent 64%)",
      "radial-gradient(52% 44% at 90% 22%, rgba(37,99,235,.30), transparent 66%)",
      "radial-gradient(56% 44% at 70% 100%, rgba(14,116,144,.28), transparent 68%)",
      "linear-gradient(170deg, #0b1d33 0%, #071426 56%, #040a14 100%)",
    ].join(", "),
    tint: "#38bdf8",
  },
  {
    id: "moss",
    name: "Moss",
    css: [
      "radial-gradient(50% 38% at 20% 4%, rgba(134,239,172,.24), transparent 64%)",
      "radial-gradient(48% 40% at 86% 18%, rgba(45,212,191,.22), transparent 64%)",
      "radial-gradient(58% 46% at 58% 102%, rgba(16,94,72,.34), transparent 68%)",
      "linear-gradient(168deg, #10231b 0%, #0a1613 58%, #060d0c 100%)",
    ].join(", "),
    tint: "#4ade80",
  },
  {
    id: "ember",
    name: "Ember",
    css: [
      "radial-gradient(48% 36% at 16% 10%, rgba(251,191,36,.26), transparent 62%)",
      "radial-gradient(50% 40% at 88% 10%, rgba(239,110,66,.26), transparent 64%)",
      "radial-gradient(62% 48% at 66% 104%, rgba(146,64,14,.36), transparent 68%)",
      "linear-gradient(164deg, #2a1a10 0%, #1a1109 58%, #0f0a06 100%)",
    ].join(", "),
    tint: "#fbbf24",
  },
  {
    id: "graphite",
    name: "Graphite",
    css: [
      "radial-gradient(54% 42% at 16% 2%, rgba(148,163,184,.20), transparent 64%)",
      "radial-gradient(48% 38% at 88% 12%, rgba(100,116,139,.22), transparent 64%)",
      "radial-gradient(56% 46% at 64% 104%, rgba(51,65,85,.42), transparent 68%)",
      "linear-gradient(170deg, #1a1d23 0%, #121417 58%, #0a0b0d 100%)",
    ].join(", "),
    tint: "#94a3b8",
  },
];

const KEY = "rag.wallpaper";

export function readWallpaper(): string {
  try {
    const v = localStorage.getItem(KEY);
    // Guard the read: a stale id from a previous build would otherwise resolve
    // to no background at all, which looks like a broken desktop.
    return v && WALLPAPERS.some((w) => w.id === v) ? v : DEFAULT_WALLPAPER;
  } catch {
    return DEFAULT_WALLPAPER;
  }
}

export function writeWallpaper(id: string): void {
  try {
    localStorage.setItem(KEY, id);
  } catch {
    /* private mode: the wallpaper just will not survive a reload */
  }
}

/** Convenience for a thumbnail, which always needs a background even for the default. */
export function previewCss(w: Wallpaper): string {
  return (
    w.css ??
    "radial-gradient(58% 42% at 14% 0%, rgba(124,106,255,.5), transparent 64%)," +
      "radial-gradient(48% 38% at 88% 6%, rgba(90,178,255,.4), transparent 62%)," +
      "radial-gradient(52% 44% at 78% 100%, rgba(236,112,236,.32), transparent 64%)," +
      "linear-gradient(168deg, #15172a 0%, #0b0c12 62%, #131020 100%)"
  );
}
