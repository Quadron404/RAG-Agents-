import { create } from "zustand";
import { setHapticsEnabled } from "./lib/haptics";

/* ============================================================================
   Types
   ========================================================================== */

export type ViewId = "chat" | "bots" | "tasks" | "computer" | "files";
export type Appearance = "system" | "light" | "dark";
export type ConnectionStatus = "connecting" | "online" | "offline";

export interface Thread {
  thread_id: string;
  user_id: string;
  title: string;
  created_at: number;
}

export interface Message {
  id: number;
  thread_id: string;
  role: "user" | "assistant";
  agent: string;
  content: string;
  data: string;
  created_at: number;
  editing?: boolean;
  pending?: boolean;
  pinned?: boolean;
}

export interface ToolCallState {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  output: string;
  error: string;
  open: boolean;
}

export interface Step {
  id: string;
  agent: string;
  title: string;
  role: string;
  status: "queued" | "working" | "done";
  tools: ToolCallState[];
  delta: string;
  report: string;
}

export type Phase = "idle" | "planning" | "working" | "synthesizing" | "done";

export interface Run {
  threadId: string;
  phase: Phase;
  plan: { objective: string; steps: Array<{ title: string; task: string; compute: string }> } | null;
  steps: Step[];
  commanderDelta: string;
  startedAt: number;
  cancel: boolean;
}

export interface ComputerState {
  running: boolean;
  booting: boolean;
  msg: string;
  error: string;
  memMb?: number;
  cpus?: number;
}

/** Kept as an alias so older imports keep compiling. */
export type VmState = ComputerState;

export interface TerminalLine {
  kind: "cmd" | "out" | "err";
  text: string;
}

export interface SysInfo {
  hostname?: string;
  uptime?: number;
  /** Read-only extras for the Settings app, all measured on the remote machine. */
  os?: string;
  kernel?: string;
  cpu?: { cores?: number; idle?: number; total?: number };
  mem?: { total_kb?: number; used_kb?: number };
  disk?: { total?: number; used?: number; free?: number };
  browser?: { running?: boolean; cdp?: boolean; pid?: number | null; start_url?: string; display?: string };
  display?: { width?: number; height?: number; size?: string };
  loadavg?: [number, number, number];
}

/* ============================================================================
   Preferences
   ========================================================================== */

const PREFS_KEY = "rag.prefs.v1";

interface PrefsState {
  appearance: Appearance;
  haptics: boolean;
  compact: boolean;
  setAppearance: (a: Appearance) => void;
  setHaptics: (v: boolean) => void;
  setCompact: (v: boolean) => void;
}

function loadPrefs(): { appearance: Appearance; haptics: boolean; compact: boolean } {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) {
      const d = JSON.parse(raw);
      return {
        appearance: d.appearance in { system: 1, light: 1, dark: 1 } ? d.appearance : "dark",
        haptics: typeof d.haptics === "boolean" ? d.haptics : true,
        compact: typeof d.compact === "boolean" ? d.compact : false,
      };
    }
  } catch {
    /* ignore */
  }
  return { appearance: "dark", haptics: true, compact: false };
}

export const usePrefs = create<PrefsState>((set, get) => ({
  ...loadPrefs(),
  setAppearance: (appearance) => {
    set({ appearance });
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({ ...get(), appearance }));
    } catch {
      /* ignore */
    }
    applyAppearance(appearance);
  },
  setHaptics: (haptics) => {
    set({ haptics });
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({ ...get(), haptics }));
    } catch {
      /* ignore */
    }
    setHapticsEnabled(haptics);
  },
  setCompact: (compact) => {
    set({ compact });
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({ ...get(), compact }));
    } catch {
      /* ignore */
    }
  },
}));

const mq = window.matchMedia?.("(prefers-color-scheme: dark)");

export function applyAppearance(appearance: Appearance) {
  const theme =
    appearance === "system" ? (mq?.matches ? "dark" : "light") : appearance;
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  meta?.setAttribute("content", theme === "dark" ? "#0b0b0d" : "#f6f6f7");
}

export function initAppearance() {
  applyAppearance(usePrefs.getState().appearance);
  mq?.addEventListener("change", () => {
    if (usePrefs.getState().appearance === "system") applyAppearance("system");
  });
}

/* ============================================================================
   UI state
   ========================================================================== */

interface MenuItem {
  label: string;
  icon?: string;
  danger?: boolean;
  onSelect: () => void;
}

export interface ReplyTo {
  threadId: string;
  msgId: number;
  text: string;
}

function loadIgnores(): { archived: string[]; pinnedConv: string | null } {
  try {
    const d = JSON.parse(localStorage.getItem("rag.ignores.v1") || "{}");
    return {
      archived: Array.isArray(d.archived) ? d.archived : [],
      pinnedConv: typeof d.pinnedConv === "string" ? d.pinnedConv : null,
    };
  } catch {
    return { archived: [], pinnedConv: null };
  }
}

function saveIgnores(archived: string[], pinnedConv: string | null) {
  try {
    localStorage.setItem("rag.ignores.v1", JSON.stringify({ archived, pinnedConv }));
  } catch {
    /* ignore */
  }
}

interface UiState {
  view: ViewId;
  convOpen: boolean;
  bot: "commander" | "researcher" | "engineer" | "navigator";
  settingsOpen: boolean;
  toasts: { id: number; kind: "info" | "err" | "ok"; text: string }[];
  menu: { x: number; y: number; items: MenuItem[] } | null;
  actionSheet: { items: MenuItem[]; title: string } | null;
  searchActive: boolean;
  replyTo: ReplyTo | null;
  pinnedConv: string | null;
  archived: string[];
  setView: (v: ViewId) => void;
  setConvOpen: (v: boolean) => void;
  setBot: (b: UiState["bot"]) => void;
  setSettingsOpen: (v: boolean) => void;
  toast: (text: string, kind?: "info" | "err" | "ok") => void;
  dismissToast: (id: number) => void;
  openMenu: (x: number, y: number, items: MenuItem[]) => void;
  closeMenu: () => void;
  openSheet: (title: string, items: MenuItem[]) => void;
  closeSheet: () => void;
  setSearchActive: (v: boolean) => void;
  setReplyTo: (r: ReplyTo | null) => void;
  clearReplyTo: () => void;
  setPinnedConv: (id: string | null) => void;
  archiveConv: (id: string) => void;
}

let toastId = 0;

export const useUi = create<UiState>((set, get) => ({
  view: "chat",
  convOpen: false,
  bot: "commander",
  settingsOpen: false,
  toasts: [],
  menu: null,
  actionSheet: null,
  searchActive: false,
  replyTo: null,
  pinnedConv: loadIgnores().pinnedConv,
  archived: loadIgnores().archived,
  setView: (view) => set({ view, convOpen: false }),
  setConvOpen: (convOpen) => set({ convOpen }),
  setBot: (bot) => set({ bot }),
  setSettingsOpen: (settingsOpen) => set({ settingsOpen }),
  toast: (text, kind = "info") => {
    const id = ++toastId;
    set({ toasts: [...get().toasts, { id, kind, text }] });
    setTimeout(() => get().dismissToast(id), 3200);
  },
  dismissToast: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  openMenu: (x, y, items) => set({ menu: { x, y, items } }),
  closeMenu: () => set({ menu: null }),
  openSheet: (title, items) => set({ actionSheet: { title, items } }),
  closeSheet: () => set({ actionSheet: null }),
  setSearchActive: (searchActive) => set({ searchActive }),
  setReplyTo: (replyTo) => set({ replyTo }),
  clearReplyTo: () => set({ replyTo: null }),
  setPinnedConv: (id) => {
    set({ pinnedConv: id });
    saveIgnores(get().archived, id);
  },
  archiveConv: (id) => {
    const archived = get().archived.includes(id) ? get().archived : [...get().archived, id];
    set({ archived });
    saveIgnores(archived, get().pinnedConv);
  },
}));

/* ============================================================================
   Agent catalog — original, distinct visual identities
   ========================================================================== */

export interface BotDef {
  id: "commander" | "researcher" | "engineer" | "navigator";
  name: string;
  role: string;
  blurb: string;
  tags: string[];
  glyph: string;
  gradient: string;
  color: string;
  goal: string;
}

export const BOTS: BotDef[] = [
  {
    id: "commander",
    name: "Commander",
    role: "Coordinator",
    blurb:
      "Plans each objective, assigns specialists, and synthesizes their work into a single clear answer. The center of your workforce.",
    tags: ["orchestration", "synthesis"],
    glyph: "◇",
    gradient: "var(--bot-commander)",
    color: "var(--violet)",
    goal: "Turn your request into a finished result.",
  },
  {
    id: "researcher",
    name: "Researcher",
    role: "Web & sources",
    blurb:
      "Searches the web, fetches sources, and extracts the facts your task needs — cited and summarized.",
    tags: ["web_search", "sources"],
    glyph: "⌕",
    gradient: "var(--bot-researcher)",
    color: "var(--blue)",
    goal: "Find and verify the latest information.",
  },
  {
    id: "engineer",
    name: "Engineer",
    role: "Code & files",
    blurb:
      "Writes and runs code, reads and edits files, and works the terminal inside your private computer.",
    tags: ["shell", "files", "code"],
    glyph: "{ }",
    gradient: "var(--bot-engineer)",
    color: "var(--amber)",
    goal: "Build, compute, and organise your files.",
  },
  {
    id: "navigator",
    name: "Navigator",
    role: "Browser & screens",
    blurb:
      "Drives the persistent browser, watches the live screen, and interacts with real pages for you.",
    tags: ["browser", "live screen"],
    glyph: "◎",
    gradient: "var(--bot-navigator)",
    color: "var(--green)",
    goal: "Browse the web as if you were there.",
  },
];

export function botForRole(role: string): BotDef {
  if (role === "browser") return BOTS[3];
  return BOTS[2];
}

export const BOT_BY_ID = Object.fromEntries(BOTS.map((b) => [b.id, b])) as Record<
  BotDef["id"],
  BotDef
>;