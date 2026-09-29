/**
 * Application icons.
 *
 * Drawn here rather than imported so all four share one construction: the same
 * superellipse-ish tile, the same top-left light source, the same inner shadow,
 * and glyphs built on the same 24px grid.  That is what makes a row of them
 * read as one set instead of four unrelated pictures.
 *
 * The tiles are intentionally colourful.  A desktop dock is the one place where
 * colour carries meaning -- it is how you find an app without reading -- so
 * each app gets its own hue while the lighting stays identical.
 *
 * Everything is plain SVG with no filter primitives: the gradients and the
 * glyphs are the only work, so a tile is a handful of paint operations and the
 * compositor can hold it on its own layer.
 */

import type { ReactElement } from "react";

export type IconApp = "browser" | "terminal" | "files" | "settings";

interface Palette {
  /** Top-left to bottom-right, the same direction as the highlight. */
  from: string;
  to: string;
  /** Inner glow along the top edge, sold as a lit bevel. */
  sheen: string;
  glyph: string;
}

const PALETTE: Record<IconApp, Palette> = {
  browser: { from: "#4c93ff", to: "#1d4ed8", sheen: "rgba(255,255,255,.34)", glyph: "#fff" },
  terminal: { from: "#3a4353", to: "#12161d", sheen: "rgba(255,255,255,.18)", glyph: "#5eead4" },
  files: { from: "#ffc061", to: "#e8712a", sheen: "rgba(255,255,255,.42)", glyph: "#fff" },
  settings: { from: "#a48bff", to: "#6438d6", sheen: "rgba(255,255,255,.32)", glyph: "#fff" },
};

/* ---- glyphs ---------------------------------------------------------------
   White marks with no stroke where a solid shape carries better -- a stroked
   gear at 20px turns to mush. */

function Globe() {
  return (
    <g fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <circle cx="12" cy="12" r="8.1" />
      <path d="M3.9 12h16.2" />
      <path d="M12 3.9c2.5 2.4 2.5 13.8 0 16.2" />
      <path d="M12 3.9c-2.5 2.4-2.5 13.8 0 16.2" />
    </g>
  );
}

function Chevrons() {
  return (
    <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6.4 8.6 9.7 12l-3.3 3.4" />
      <path d="M12.4 15.4h5.2" />
    </g>
  );
}

function Folder() {
  return (
    <path
      fill="currentColor"
      d="M4.3 7.6c0-1 .8-1.8 1.8-1.8h3.3c.6 0 1.1.2 1.5.7l.9 1.1c.2.2.5.4.8.4h5.4c1 0 1.8.8 1.8 1.8v7.4c0 1-.8 1.8-1.8 1.8H6.1c-1 0-1.8-.8-1.8-1.8z"
    />
  );
}

/** A real gear: a ring plus eight rounded teeth, so it stays legible when small. */
function Gear() {
  const teeth = Array.from({ length: 8 }, (_, i) => i * 45);
  return (
    <g>
      {teeth.map((deg) => (
        <rect
          key={deg}
          x="10.5"
          y="2.9"
          width="3"
          height="4.2"
          rx="1.3"
          fill="currentColor"
          transform={`rotate(${deg} 12 12)`}
        />
      ))}
      <circle cx="12" cy="12" r="6.1" fill="none" stroke="currentColor" strokeWidth="2.4" />
    </g>
  );
}

const GLYPHS: Record<IconApp, () => ReactElement> = {
  browser: Globe,
  terminal: Chevrons,
  files: Folder,
  settings: Gear,
};

/**
 * The tile.  `size` is the box; the glyph is sized from it rather than fixed, so
 * a dock icon and a window-title icon stay optically identical.
 */
export function AppIcon({ app, size = 44, className = "" }: { app: IconApp; size?: number; className?: string }) {
  const p = PALETTE[app];
  const Glyph = GLYPHS[app];
  const uid = `ai-${app}`;
  // The glyph sits on a 24-unit grid inside the tile; 0.62 keeps it from
  // crowding the bevel at dock sizes and from vanishing in a title bar.
  const glyphBox = Math.round(size * 0.62);
  const inset = Math.round((size - glyphBox) / 2);

  return (
    <svg
      className={`appicon ${className}`.trim()}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={`${uid}-bg`} x1="0" y1="0" x2="0.35" y2="1">
          <stop offset="0" stopColor={p.from} />
          <stop offset="1" stopColor={p.to} />
        </linearGradient>
        <linearGradient id={`${uid}-sheen`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={p.sheen} />
          <stop offset="0.55" stopColor="rgba(255,255,255,0)" />
        </linearGradient>
      </defs>

      <rect x="0" y="0" width={size} height={size} rx={Math.round(size * 0.26)} fill={`url(#${uid}-bg)`} />
      {/* Lit bevel across the top half, then a dark settle at the bottom edge. */}
      <rect x="0" y="0" width={size} height={Math.round(size * 0.58)} rx={Math.round(size * 0.26)} fill={`url(#${uid}-sheen)`} />
      <rect
        x="0.5"
        y={size * 0.62}
        width={size - 1}
        height={size * 0.38 - 0.5}
        rx={Math.round(size * 0.2)}
        fill="rgba(0,0,0,.16)"
      />
      <g color={p.glyph} transform={`translate(${inset} ${inset})`}>
        <svg width={glyphBox} height={glyphBox} viewBox="0 0 24 24">
          <Glyph />
        </svg>
      </g>
    </svg>
  );
}
