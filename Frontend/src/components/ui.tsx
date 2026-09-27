import { useCallback, useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Copy,
  Folder,
  Info,
  MessageSquare,
  MousePointer2,
  Pencil,
  Pin,
  Reply,
  Send,
  Share2,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import { useUi } from "../store";
import { haptic } from "../lib/haptics";

/* ---- Avatar ------------------------------------------------------------ */

export function Avatar({
  gradient,
  glyph,
  size = 34,
  status,
  className = "",
}: {
  gradient: string;
  glyph: string | ReactNode;
  size?: number;
  status?: "online" | "thinking" | "working" | "waiting" | "completed" | "offline";
  className?: string;
}) {
  return (
    <div
      className={`msg__ava ${className}`.trim()}
      style={{ background: gradient, width: size, height: size, fontSize: Math.round(size * 0.4) }}
      aria-hidden
    >
      {glyph}
      {status ? <span className={`dot dot--${status} msg-ava-dot`} /> : null}
    </div>
  );
}

/* ---- Icon button -------------------------------------------------------- */

type LucideIcon = React.ComponentType<{ size?: number | string; strokeWidth?: number | string }>;

export function IconButton({
  icon: Icon,
  onClick,
  title,
  active,
  className = "",
  badge,
  ariaLabel,
  disabled,
}: {
  icon: LucideIcon;
  onClick: (e: React.MouseEvent) => void;
  title?: string;
  active?: boolean;
  className?: string;
  badge?: boolean;
  ariaLabel?: string;
  disabled?: boolean;
}) {
  return (
    <button
      className={`icon-btn${active ? " icon-btn--toggle" : ""} ${className}`.trim()}
      onClick={(e) => {
        if (disabled) return;
        haptic("light");
        onClick(e);
      }}
      title={title}
      aria-label={ariaLabel || title}
      aria-pressed={active}
      disabled={disabled}
    >
      <Icon size={19} strokeWidth={1.9} />
      {badge ? <span className="badge-dot" /> : null}
    </button>
  );
}

/* ---- Spinner ------------------------------------------------------------ */

export function Spinner({ size = 20 }: { size?: number }) {
  return <div className="spinner" style={{ width: size, height: size }} aria-label="Loading" role="status" />;
}

/* ---- Switch -------------------------------------------------------------- */

export function Switch({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={`switch${on ? " switch--on" : ""}`}
      onClick={(e) => {
        e.stopPropagation();
        haptic("medium");
        onChange(!on);
      }}
    >
      <span className="switch__thumb" />
    </button>
  );
}

/* ---- Pills --------------------------------------------------------------- */

export function Pill({
  children,
  tone,
}: {
  children: ReactNode;
  tone?: "accent" | "green" | "amber" | "red" | "default";
}) {
  return <span className={`pill${tone && tone !== "default" ? ` pill--${tone}` : ""}`}>{children}</span>;
}

/* ---- Toasts -------------------------------------------------------------- */

export function ToastHost() {
  const toasts = useUi((s) => s.toasts);
  const dismiss = useUi((s) => s.dismissToast);
  return createPortal(
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast${t.kind === "err" ? " toast--err" : ""}`}>
          {t.kind === "err" ? (
            <AlertCircle size={16} style={{ flex: "0 0 auto" }} />
          ) : t.kind === "ok" ? (
            <CheckCircle2 size={16} style={{ flex: "0 0 auto", color: "var(--green)" }} />
          ) : (
            <Info size={16} style={{ flex: "0 0 auto", color: "var(--accent)" }} />
          )}
          <span>{t.text}</span>
          <button className="act-btn" onClick={() => dismiss(t.id)} aria-label="Dismiss">
            <X size={13} />
          </button>
        </div>
      ))}
    </div>,
    document.body
  );
}

/* ---- Sheet ---------------------------------------------------------------- */

export function Sheet({
  open,
  onClose,
  title,
  children,
  placement = "side",
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  placement?: "side" | "center";
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return createPortal(
    <>
      <div className={`sheet__scrim sheet__scrim--open`} onClick={onClose} />
      <div
        className={`sheet__card sheet__card--${placement} ${open ? "sheet__open" : ""}`}
        role="dialog"
        aria-modal="true"
      >
        <div className="sheet__grab" />
        <div className="sheet__head">
          <div className="sheet__title">{title}</div>
          <IconButton icon={X} onClick={onClose} title="Close" ariaLabel="Close" />
        </div>
        <div className="sheet__body">{children}</div>
      </div>
    </>,
    document.body
  );
}

/* ---- Context menu ---------------------------------------------------------- */

export function ContextMenuHost() {
  const menu = useUi((s) => s.menu);
  const close = useUi((s) => s.closeMenu);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu) return;
    const l = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (ref.current && !ref.current.contains(t)) close();
    };
    const r = () => close();
    window.addEventListener("mousedown", l);
    window.addEventListener("scroll", r, true);
    return () => {
      window.removeEventListener("mousedown", l);
      window.removeEventListener("scroll", r, true);
    };
  }, [menu, close]);

  if (!menu) return null;

  const ICONS: Record<string, LucideIcon> = {
    copy: Copy,
    reply: Reply,
    edit: Pencil,
    share: Share2,
    pin: Pin,
    trash: Trash2,
    check: Check,
    select: MousePointer2,
    send: Send,
    chat: MessageSquare,
    term: Terminal,
    files: Folder,
  };

  const x = Math.min(menu.x, window.innerWidth - 210);
  const y = Math.min(menu.y, window.innerHeight - menu.items.length * 46 - 20);

  return createPortal(
    <>
      <div className="menu-scrim" onClick={close} />
      <div className="menu" ref={ref} style={{ left: Math.max(8, x), top: Math.max(8, y) }}>
        {menu.items.map((item, i) => {
          const Icon = item.icon ? ICONS[item.icon] : undefined;
          return (
            <button
              key={i}
              className={`menu__item${item.danger ? " menu__item--danger" : ""}`}
              onClick={() => {
                haptic("light");
                close();
                item.onSelect();
              }}
            >
              {Icon ? <Icon size={16} /> : null}
              {item.label}
            </button>
          );
        })}
      </div>
    </>,
    document.body
  );
}

/* ---- Bottom action sheet (mobile) ------------------------------------------ */

export function ActionSheetHost() {
  const sheet = useUi((s) => s.actionSheet);
  const close = useUi((s) => s.closeSheet);

  if (!sheet) return null;
  return createPortal(
    <>
      <div className="menu-scrim" style={{ zIndex: 165 }} onClick={close} />
      <div
        className={`actionsheet${sheet ? " actionsheet--open" : ""}`}
        role="dialog"
        aria-label={sheet.title}
      >
        <div className="actionsheet__title">{sheet.title}</div>
        {sheet.items.map((item, i) => (
          <button
            key={i}
            className={`menu__item${item.danger ? " menu__item--danger" : ""}`}
            style={{ padding: "13px 12px" }}
            onClick={() => {
              haptic("medium");
              close();
              item.onSelect();
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
    </>,
    document.body
  );
}

/* ---- Long-press helper ------------------------------------------------------ */

export function useLongPress(callback: () => void, ms = 420) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startedOnNode = useRef(false);

  const start = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      startedOnNode.current = e.pointerType !== "mouse";
      timer.current = setTimeout(() => {
        haptic("medium");
        callback();
      }, ms);
    },
    [callback, ms]
  );

  const cancel = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => {
    return cancel;
  }, [cancel]);

  return {
    onPointerDown: start,
    onPointerUp: cancel,
    onPointerLeave: cancel,
    onPointerMove: (e: React.PointerEvent) => {
      if (timer.current && startedOnNode.current && e.pointerType === "mouse") {
        // allow mouse move without cancel
      }
    },
  };
}

/* ---- Action icon map used by chatbot actions ---------------------------- */

export type { LucideIcon };