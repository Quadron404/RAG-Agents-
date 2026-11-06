import { type ReactNode, forwardRef } from "react";
import { haptic } from "../lib/haptics";

/* ============================================================================
   Reusable surface & component primitives (PROMPT-13)

   Single source of truth for the interactive building blocks used across
   screens: Button, Card, Input, Badge, Section. Variants compose the existing
   theme.css classes (`.btn`, `.btn--*`, `.pill`, `.pill--*`) and CSS
   variables, so nothing duplicates or contradicts the design system. Overlay
   hosts (Sheet, ToastHost, ContextMenuHost, ActionSheetHost) live in ui.tsx
   and are reused as-is.
   ========================================================================== */

/* ---- Button --------------------------------------------------------------- */

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "ghost" | "soft" | "danger";
  size?: "sm" | "md" | "lg";
  quiet?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className = "", variant = "primary", size = "md", quiet = false, disabled, children, onClick, ...rest }, ref) => {
    const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
      if (disabled) return;
      haptic("light");
      onClick?.(e);
    };

    const variantMap: Record<string, string> = {
      primary: "btn--primary",
      soft: "btn--soft",
      danger: "btn--danger",
      ghost: "btn--ghost",
    };
    const sizeMap: Record<string, string> = {
      sm: "btn--sm",
      md: "",
      lg: "btn--lg",
    };

    return (
      <button
        ref={ref}
        className={`btn ${variantMap[variant]} ${sizeMap[size]} ${quiet ? "btn--quiet" : ""} ${className}`.trim()}
        onClick={handleClick}
        disabled={disabled}
        {...rest}
      >
        {children}
      </button>
    );
  },
);

Button.displayName = "Button";

/* ---- Card ---------------------------------------------------------------- */

export interface CardProps {
  children: ReactNode;
  className?: string;
  raised?: boolean;
}

export function Card({ children, className = "", raised = false }: CardProps) {
  return (
    <div
      style={{
        border: "1px solid var(--line)",
        borderRadius: 12,
        background: "var(--surface-raised)",
        boxShadow: raised ? "var(--shadow-2)" : "var(--shadow-1)",
        padding: 16,
      }}
      className={className}
    >
      {children}
    </div>
  );
}

/* ---- Input ---------------------------------------------------------------- */

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
}

export function Input({ label, className = "", ...rest }: InputProps) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {label ? (
        <label style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-2)" }}>{label}</label>
      ) : null}
      <input
        style={{
          flex: 1,
          minWidth: 0,
          background: "var(--bg3)",
          border: "1px solid transparent",
          borderRadius: 10,
          color: "var(--text)",
          padding: "8px 12px",
          fontSize: 14,
          outline: "none",
        }}
        className={className}
        {...rest}
      />
    </div>
  );
}

/* ---- Badge / pill -------------------------------------------------------- */

export interface BadgeProps {
  children: ReactNode;
  tone?: "accent" | "green" | "amber" | "red" | "default";
  className?: string;
}

export function Badge({ children, tone = "default", className = "" }: BadgeProps) {
  return (
    <span className={`pill${tone !== "default" ? ` pill--${tone}` : ""} ${className}`}>
      {children}
    </span>
  );
}

/* ---- Section heading ----------------------------------------------------- */

export function Section({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div
      style={{
        fontSize: "11px",
        fontWeight: 800,
        letterSpacing: "0.09em",
        textTransform: "uppercase",
        color: "var(--text-3)",
        marginBottom: 6,
      }}
      className={className}
    >
      {children}
    </div>
  );
}
