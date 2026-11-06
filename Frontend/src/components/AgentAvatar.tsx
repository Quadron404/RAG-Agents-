import type { ReactNode } from "react";

/** Canonical specialist status.  Derived from backend execution state only. */
export type AvatarStatus =
  | "idle"
  | "online"
  | "thinking"
  | "working"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled"
  | "offline";

/** Animation mode: how the dot grid moves. */
export type AvatarAnimation = "none" | "idle" | "thinking" | "working";

export interface AgentAvatarProps {
  /** Stable, backend-assigned agent ID.  Drives the deterministic accent. */
  agentId: string;
  /** Human-readable name for the accessible label. */
  name?: string;
  /** Role shown in the accessible label. */
  role?: string;
  /** Current backend execution state. */
  status?: AvatarStatus;
  /** Animation mode.  Defaults to idle when status is idle/waiting/completed/failed/cancelled/offline. */
  animation?: AvatarAnimation;
  /** Display size in px.  Dot geometry is preserved at any size. */
  size?: number;
  /** When true, motion is reduced or disabled. */
  reducedMotion?: boolean;
  /** When true, the avatar is decorative and hidden from assistive tech. */
  decorative?: boolean;
  className?: string;
}

/** Deterministic accent token derived from agentId.  Same agent, same colour everywhere. */
export function agentAccent(agentId: string): string {
  let hash = 0;
  for (let i = 0; i < agentId.length; i++) {
    hash = (hash * 31 + agentId.charCodeAt(i)) | 0;
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 70%, 55%)`;
}

/** Accessible accent palette.  Colour is a secondary differentiator, never the only status cue. */
const ACCENT_PALETTE: Record<string, string> = {
  chief: "var(--violet)",
  researcher: "var(--blue)",
  engineer: "var(--amber)",
  navigator: "var(--green)",
};

export function agentAccentToken(agentId: string): string {
  return ACCENT_PALETTE[agentId] ?? agentAccent(agentId);
}

const DOT_COUNT = 9;
const GRID_SIZE = 3;

/** Renders one dot cell in the 3x3 grid. */
function DotCell({
  index,
  size,
  opacity,
  accent,
  reducedMotion,
  animation,
  status,
}: {
  index: number;
  size: number;
  opacity: number;
  accent: string;
  reducedMotion: boolean;
  animation: AvatarAnimation;
  status: AvatarStatus;
}) {
  const dotSize = Math.max(2, Math.round(size / 10));
  const isIdle = animation === "idle" && !reducedMotion;
  const isThinking = animation === "thinking" && !reducedMotion;
  const isWorking = animation === "working" && !reducedMotion;

  // Each dot has a deterministic phase offset so the 3x3 grid illuminates in a
  // restrained sequence rather than all at once.
  const phase = (index / DOT_COUNT) * 2 * Math.PI;

  let dotOpacity = opacity;
  let dotTransform = "scale(1)";
  let dotFilter = "";
  let animationName = "";
  let animationDur = "";

  if (status === "completed") {
    dotOpacity = 1;
    dotTransform = "scale(1)";
  } else if (status === "failed" || status === "cancelled") {
    dotOpacity = 0.5;
  } else if (status === "offline") {
    dotOpacity = 0.25;
  } else if (isIdle) {
    // Slow, restrained idle illumination: dots breathe in sequence.
    animationName = "agentAvatarIdle";
    animationDur = "6s";
    dotOpacity = 0.35 + 0.45 * Math.abs(Math.sin(phase));
  } else if (isThinking) {
    // Thinking: a gentle sweep across the grid.
    animationName = "agentAvatarThinking";
    animationDur = "2.2s";
    dotOpacity = 0.4 + 0.4 * Math.abs(Math.sin(phase + Date.now() / 400));
  } else if (isWorking) {
    // Working: brighter, faster pulse.
    dotOpacity = 0.6 + 0.3 * Math.abs(Math.sin(phase + Date.now() / 200));
    dotTransform = "scale(1.05)";
  }

  const style: React.CSSProperties = {
    width: dotSize,
    height: dotSize,
    borderRadius: "50%",
    background: accent,
    opacity: dotOpacity,
    transform: dotTransform,
    filter: dotFilter,
    transition: "opacity 0.2s ease, transform 0.2s ease",
  };

  if (animationName && !reducedMotion) {
    style.animation = `${animationName} ${animationDur} ease-in-out infinite`;
    style.animationDelay = `${phase * 300}ms`;
  }

  return (
    <div
      className="agent-avatar__dot"
      style={style}
      aria-hidden
    />
  );
}

/**
 * AgentAvatar — canonical 3x3 dot-grid avatar for specialist identities.
 *
 * The mark is a black rounded-square surface with a subtle glass edge and nine
 * white/accent dots in a symmetrical 3x3 grid.  Colour is a secondary
 * differentiator: status is always conveyed by animation + dot opacity +
 * accessible text, never colour alone.
 *
 * Visual identity is derived from a deterministic hash of `agentId`, so the
 * same specialist looks consistent across navigation, chat, Team, cards, and
 * history — without any hard-coded role map.
 */
export function AgentAvatar({
  agentId,
  name,
  role,
  status = "idle",
  animation,
  size = 40,
  reducedMotion = false,
  decorative = false,
  className = "",
}: AgentAvatarProps): ReactNode {
  const accent = agentAccentToken(agentId);
  const surfaceSize = size;
  const padding = Math.round(size * 0.14);
  const gridSize = size - padding * 2;
  const dotSize = Math.max(2, Math.round(gridSize / 10));
  const gap = Math.round((gridSize - dotSize * GRID_SIZE) / (GRID_SIZE - 1));

  // Determine animation from status when not explicitly provided.
  let resolvedAnimation = animation;
  if (!resolvedAnimation) {
    switch (status) {
      case "idle":
      case "online":
      case "completed":
      case "failed":
      case "cancelled":
      case "offline":
        resolvedAnimation = "none";
        break;
      case "waiting":
        resolvedAnimation = "idle";
        break;
      case "thinking":
        resolvedAnimation = "thinking";
        break;
      case "working":
        resolvedAnimation = "working";
        break;
    }
  }

  const dotOpacity =
    status === "completed" ? 1 : status === "offline" ? 0.2 : status === "failed" || status === "cancelled" ? 0.4 : 0.8;
  const accessibleName = decorative
    ? undefined
    : `${name ?? agentId}${role ? `, ${role}` : ""}${status ? `, ${status}` : ""}`;

  const prefersReduced = reducedMotion || (typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);

  return (
    <div
      className={`agent-avatar ${className}`.trim()}
      style={{
        width: surfaceSize,
        height: surfaceSize,
        borderRadius: Math.round(size * 0.22),
        background: "#0a0a0b",
        border: "1px solid var(--line-strong)",
        boxShadow: "0 1px 3px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.10)",
        display: "grid",
        placeItems: "center",
        position: "relative",
        overflow: "hidden",
      }}
      aria-label={accessibleName}
      aria-hidden={decorative || undefined}
    >
      {/* Glass edge highlight */}
      <div
        className="agent-avatar__glass"
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: Math.round(size * 0.34),
          background: "linear-gradient(to bottom, rgba(255,255,255,0.14), rgba(255,255,255,0.00))",
          pointerEvents: "none",
        }}
        aria-hidden
      />
      {/* 3x3 dot grid */}
      <div
        className="agent-avatar__grid"
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${GRID_SIZE}, ${dotSize}px)`,
          gridTemplateRows: `repeat(${GRID_SIZE}, ${dotSize}px)`,
          gap: gap,
          position: "relative",
          zIndex: 1,
        }}
      >
        {[...Array(DOT_COUNT)].map((_, i) => (
          <DotCell
            key={i}
            index={i}
            size={dotSize}
            opacity={dotOpacity}
            accent={accent}
            reducedMotion={prefersReduced}
            animation={resolvedAnimation}
            status={status}
          />
        ))}
      </div>
      <style>
        {`
          @keyframes agentAvatarIdle {
            0%, 100% { opacity: 0.35; transform: scale(1); }
            50% { opacity: 0.8; transform: scale(1.12); }
          }
          @keyframes agentAvatarThinking {
            0%, 100% { opacity: 0.4; transform: scale(1); }
            50% { opacity: 0.9; transform: scale(1.18); }
          }
        `}
      </style>
    </div>
  );
}
