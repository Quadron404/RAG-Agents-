import { create } from "zustand";
import { useUi } from "./store";
import type { ComputerState, ConnectionStatus, Message, Run, Step, SysInfo, TerminalLine, Thread, ToolCallState } from "./store";

/* ============================================================================
   Backend addressing — same-origin in dev (Vite proxy) and production
   (FastAPI static mount); override with VITE_BACKEND for direct access.
   ========================================================================== */

const BACKEND: string = (import.meta.env.VITE_BACKEND as string) || "";

export const api = (path: string): string => (BACKEND ? BACKEND + path : path);

/**
 * fetch() for API calls, with the session cookie attached.
 *
 * The app is served from the same origin as the screen -- the Quick Tunnel
 * publishes this process, and the Computer view opens /websockify on the same
 * host -- so the HttpOnly session cookie is what authorises both.  "include" is
 * what makes the browser attach it, and routing every call through one helper
 * means no endpoint can quietly end up without a credential.
 */
export const apiFetch = (path: string, init: RequestInit = {}): Promise<Response> =>
  fetch(api(path), { credentials: "include", ...init });

export function wsUrl(path: string): string {
  if (BACKEND) {
    return BACKEND.replace(/^http:/, "ws:").replace(/^https:/, "wss:") + path;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

export const USER = "me";

/* ============================================================================
   Queued tool calls (promise-based) resolved by ws tool_result messages
   ========================================================================== */

interface PendingTool {
  resolve: (r: ToolResult) => void;
  timer: ReturnType<typeof setTimeout>;
}

export interface ToolResult {
  output: string;
  error: string;
  title: string;
  url: string;
  image: string;
  format: string;
}

const pendingTools = new Map<string, PendingTool>();
let reqSeq = 0;

/* ============================================================================
   Core store
   ========================================================================== */

interface RunEvents {
  type: "plan" | "worker_start" | "delta" | "tool_start" | "tool_output" | "tool_result" | "synthesize";
  [k: string]: unknown;
}

type WireEvent = { event?: RunEvents } & Record<string, any>;

interface CoreState {
  status: ConnectionStatus;
  userId: string;
  threads: Thread[];
  activeThreadId: string | null;
  messages: Record<string, Message[]>;
  run: Run | null;
  computer: ComputerState;
  screen: string | null;
  sysinfo: SysInfo | null;
  terminal: TerminalLine[];
  webTitle: string;
  webUrl: string;
  vmPath: string;

  connect: () => void;
  refreshThreads: () => Promise<void>;
  newChat: () => Promise<void>;
  openThread: (id: string) => Promise<void>;
  loadMessages: (id: string) => Promise<void>;
  send: (text: string) => void;
  stopRun: () => void;
  requestScreen: () => void;
  computerStart: () => Promise<void>;
  computerStop: () => Promise<void>;
  computerStatus: () => void;
  refreshSysinfo: () => Promise<void>;
  callTool: (tool: string, args: Record<string, unknown>, timeout?: number) => Promise<ToolResult>;
  shell: (cmd: string) => Promise<void>;
  clearTerminal: () => void;
  setVmPath: (p: string) => void;
  setWeb: (title: string, url: string) => void;
}

let ws: WebSocket | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let finalizeTimer: ReturnType<typeof setTimeout> | null = null;

const emptyComputer: ComputerState = { running: false, booting: false, msg: "computer is offline", error: "" };

function pushMsg(threadId: string, msg: Message) {
  useCore.setState((s) => {
    const list = s.messages[threadId] || [];
    return { messages: { ...s.messages, [threadId]: [...list, msg] } };
  });
}

function patchRun(patch: Partial<Run>) {
  const { run } = useCore.getState();
  if (!run) return;
  useCore.setState({ run: { ...run, ...patch } });
}

function finalize() {
  if (finalizeTimer) clearTimeout(finalizeTimer);
  finalizeTimer = null;
  const core = useCore.getState();
  const run = core.run;
  if (!run || run.cancel || run.phase === "done") return;
  const steps = run.steps.map((s) => ({ ...s, status: "done" as const }));
  useCore.setState({ run: { ...run, phase: "done", steps } });
  core.refreshThreads();
  if (core.activeThreadId) core.loadMessages(core.activeThreadId);
}

function scheduleFinalize() {
  if (finalizeTimer) clearTimeout(finalizeTimer);
  finalizeTimer = setTimeout(finalize, 1600);
}

function setRun(threadId: string) {
  const { run } = useCore.getState();
  if (run && run.threadId === threadId && run.phase !== "done") {
    return run;
  }
  const fresh: Run = {
    threadId,
    phase: "planning",
    plan: null,
    steps: [],
    commanderDelta: "",
    startedAt: Date.now(),
    cancel: false,
  };
  useCore.setState({ run: fresh });
  return fresh;
}

/* --- event wiring --------------------------------------------------------- */

function handleWireEvent(d: WireEvent) {
  const core = useCore.getState();
  switch (d.type) {
    case "connected":
      useCore.setState({ status: "online", userId: d.user_id || USER });
      core.refreshThreads();
      break;
    case "thread": {
      const tid = String(d.thread_id || "");
      if (!tid) break;
      const prev = useCore.getState();
      const pending = prev.messages["__new__"] || [];
      useCore.setState((s) => {
        const messages = { ...s.messages };
        const merged = [...(messages[tid] || []), ...pending.map((m) => ({ ...m, thread_id: tid }))];
        messages[tid] = merged;
        delete messages["__new__"];
        return { activeThreadId: tid, messages };
      });
      useCore.getState().refreshThreads();
      break;
    }
    case "user_ack":
      if (d.thread_id) useCore.setState({ activeThreadId: d.thread_id });
      if (!core.run || core.run.threadId !== d.thread_id) setRun(d.thread_id as string);
      break;
    case "event":
    case undefined: {
      const ev = d.event;
      if (ev) handleRunEvent(ev);
      break;
    }
    case "computer": {
      const computer: ComputerState = { ...useCore.getState().computer };
      if (typeof d.running === "boolean") computer.running = d.running;
      if (d.result) {
        if (d.result.ok) {
          computer.running = true;
          computer.booting = false;
          computer.msg = d.result.msg || "computer is online";
          computer.error = "";
          if (d.result.mem_mb) computer.memMb = Number(d.result.mem_mb) || 0;
          if (d.result.cpus) computer.cpus = Number(d.result.cpus) || 0;
        } else {
          computer.booting = false;
          computer.running = false;
          computer.error = d.result.error || "the computer did not start";
          computer.msg = computer.error;
        }
      }
      useCore.setState({ computer });
      if (computer.running) {
        useCore.getState().requestScreen();
        useCore.getState().refreshSysinfo();
      }
      break;
    }
    case "screen":
      if (d.image) useCore.setState({ screen: `data:image/jpeg;base64,${d.image}` });
      break;
    case "tool_result": {
      const p = pendingTools.get(d.id);
      if (d.image) {
        useCore.setState({ screen: `data:image/jpeg;base64,${d.image}` });
        if (d.title || d.url) useCore.setState({ webTitle: String(d.title || "Untitled"), webUrl: String(d.url || "") });
      }
      if (p) {
        clearTimeout(p.timer);
        pendingTools.delete(d.id);
        p.resolve({
          output: d.output || "",
          error: d.error || "",
          title: d.title || "",
          url: d.url || "",
          image: d.image || "",
          format: d.format || "",
        });
      }
      break;
    }
    case "error":
      useUi.getState().toast(d.error || "Something went wrong.", "err");
      break;
  }
}

function handleRunEvent(ev: RunEvents) {
  const core = useCore.getState();
  const run = core.run;
  const tid = run?.threadId || core.activeThreadId;
  if (!tid) return;
  const active = run && run.threadId === core.activeThreadId;

  switch (ev.type) {
    case "plan": {
      const plan = ev.plan as Run["plan"];
      const steps: Step[] = (plan?.steps || []).map((s, i) => ({
        id: `worker-${i + 1}`,
        agent: `worker-${i + 1}`,
        title: s.title,
        role: s.compute === "browser" ? "browser" : "worker",
        status: "queued",
        tools: [],
        delta: "",
        report: "",
      }));
      const r = active ? run || setRun(tid) : setRun(tid);
      useCore.setState({
        run: {
          ...r,
          plan: plan || r.plan,
          steps: steps.length ? steps : r.steps,
          phase: "planning",
          cancel: false,
        },
      });
      break;
    }
    case "worker_start": {
      const agent = String(ev.agent || "");
      const title = String(ev.title || "Task");
      const role = String(ev.role || "worker");
      const r = active ? run || setRun(tid) : setRun(tid);
      const idx = r.steps.findIndex((s) => s.id === agent);
      let steps = r.steps;
      if (idx >= 0) {
        steps = r.steps.map((s) =>
          s.id === agent ? { ...s, title: s.title || title, role, status: "working" as const } : s
        );
      } else {
        steps = [...r.steps, { id: agent, agent, title, role, status: "working", tools: [], delta: "", report: "" }];
      }
      useCore.setState({ run: { ...r, steps, phase: "working" } });
      break;
    }
    case "delta": {
      const agent = String(ev.agent || "commander");
      const text = String(ev.text || "");
      const r = (active ? run : null) || setRun(tid);
      useCore.setState({
        run: {
          ...r,
          commanderDelta:
            agent === "commander" ? r.commanderDelta + text : r.commanderDelta,
          steps: r.steps.map((s) =>
            s.id === agent ? { ...s, delta: s.delta + text } : s
          ),
        },
      });
      break;
    }
    case "tool_start": {
      const agent = String(ev.agent || "commander");
      const id = String(ev.id || `t-${Date.now()}`);
      const tool = String(ev.tool || "");
      const args = (ev.args as Record<string, unknown>) || {};
      const r = (active ? run : null) || setRun(tid);
      const tc: ToolCallState = { id, tool, args, status: "running", output: "", error: "", open: false };
      useCore.setState({
        run: {
          ...r,
          phase: "working",
          steps: r.steps.map((s) =>
            s.id === agent ? { ...s, status: "working", tools: [...s.tools, tc] } : s
          ),
        },
      });
      if (tool === "shell" && !active) {
        useCore.getState().shell(String(args.command || ""));
      } else if (tool === "shell") {
        useCore.setState((s) => ({
          terminal: [
            ...s.terminal,
            { kind: "cmd", text: String(args.command || "") },
          ],
        }));
      }
      break;
    }
    case "tool_output": {
      const id = String(ev.id || "");
      const content = String(ev.content || "");
      const r = (active ? run : null);
      if (!r) break;
      useCore.setState({
        run: {
          ...r,
          steps: r.steps.map((s) => ({
            ...s,
            tools: s.tools.map((t) =>
              t.id === id ? { ...t, output: t.output + content } : t
            ),
          })),
        },
      });
      break;
    }
    case "tool_result": {
      const r = (active ? run : null);
      if (!r) break;
      const id = String(ev.id || "");
      const output = String(ev.output || "");
      const error = String(ev.error || "");
      const status: "ok" | "error" = error ? "error" : "ok";
      useCore.setState({
        run: {
          ...r,
          steps: r.steps.map((s) => ({
            ...s,
            tools: s.tools.map((t) =>
              t.id === id ? { ...t, status, output: t.output || output, error } : t
            ),
          })),
        },
      });
      break;
    }
    case "synthesize":
      patchRun({ phase: "synthesizing" });
      scheduleFinalize();
      break;
  }
}

/* ============================================================================
   Store
   ========================================================================== */

export const useCore = create<CoreState>((set, get) => ({
  status: "connecting",
  userId: USER,
  threads: [],
  activeThreadId: null,
  messages: {},
  run: null,
  computer: { running: false, booting: false, msg: "", error: "" },
  screen: null,
  sysinfo: null,
  terminal: [],
  webTitle: "Search",
  webUrl: "",
  vmPath: "/workspace",

  connect() {
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    set({ status: "connecting" });
    try {
      ws = new WebSocket(wsUrl(`/ws/${USER}`));
    } catch {
      scheduleReconnect();
      return;
    }
    ws.onopen = () => {
      set({ status: "online" });
      get().computerStatus();
    };
    ws.onclose = () => {
      set({ status: "offline" });
      pendingTools.forEach((p) => {
        clearTimeout(p.timer);
        p.resolve({ output: "", error: "connection lost", title: "", url: "", image: "", format: "" });
      });
      pendingTools.clear();
      scheduleReconnect();
    };
    ws.onerror = () => {
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
    ws.onmessage = (m) => {
      let d: WireEvent;
      try {
        d = JSON.parse(m.data);
      } catch {
        return;
      }
      handleWireEvent(d);
    };
  },

  async refreshThreads() {
    try {
      const r = await apiFetch(`/threads?user_id=${USER}`);
      const d = await r.json();
      set({ threads: d.threads || [] });
      if (!get().activeThreadId && d.threads?.length) {
        get().openThread(d.threads[0].thread_id);
      }
    } catch {
      /* retry on next connect */
    }
  },

  async newChat() {
    try {
      const r = await apiFetch("/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: USER, title: "New conversation" }),
      });
      const d = await r.json();
      set({ activeThreadId: d.thread_id, run: null, screen: null });
      await get().refreshThreads();
      useUi.getState().setView("chat");
    } catch {
      useUi.getState().toast("Could not create a new conversation.", "err");
    }
  },

  async openThread(id) {
    set({ activeThreadId: id });
    useUi.getState().setView("chat");
    await get().loadMessages(id);
  },

  async loadMessages(id) {
    try {
      const r = await apiFetch(`/threads/${id}/messages`);
      const d = await r.json();
      const server = (d.messages || []) as Message[];
      const cur = get().messages[id] || [];
      const pending = cur.filter((m) => m.pending && !server.some((x) => x.role === "user" && x.content === m.content));
      set({ messages: { ...get().messages, [id]: [...server, ...pending] } });
    } catch {
      /* ignore */
    }
  },

  send(text) {
    const { status, activeThreadId, run } = get();
    if (status !== "online") {
      useUi.getState().toast("Reconnecting to the workforce…", "err");
      return;
    }
    if (run && run.threadId === activeThreadId && run.phase !== "done" && run.phase !== "idle") {
      useUi.getState().toast("The workforce is already working on your request.", "info");
      return;
    }
    const tid = activeThreadId || null;
    if (finalizeTimer) clearTimeout(finalizeTimer);
    pushMsg(tid || "__new__", {
      id: -Date.now(),
      thread_id: tid || "__new__",
      role: "user",
      agent: "user",
      content: text,
      data: "{}",
      created_at: Date.now() / 1000,
      pending: true,
    });
    setRun(tid || "__new__");
    ws?.send(JSON.stringify({ type: "message", text, thread_id: tid }));
  },

  stopRun() {
    const { run } = get();
    if (!run) return;
    patchRun({ cancel: true, phase: "done" });
    if (finalizeTimer) clearTimeout(finalizeTimer);
  },

  requestScreen() {
    if (ws?.readyState === 1) {
      ws.send(JSON.stringify({ type: "screen" }));
    }
  },

  async computerStart() {
    set({ computer: { ...emptyComputer, booting: true, msg: "starting the computer…" } });
    try {
      const r = await apiFetch("/computer/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: USER }),
      });
      const d = await r.json();
      if (d.ok) {
        set({
          computer: {
            running: true,
            booting: false,
            msg: d.msg || "computer is online",
            error: "",
            memMb: Number(d.mem_mb) || 0,
            cpus: Number(d.cpus) || 0,
          },
        });
        get().requestScreen();
        get().refreshSysinfo();
      } else {
        set({ computer: { ...emptyComputer, booting: false, error: d.error || "failed to start", msg: d.error || "failed to start" } });
        useUi.getState().toast(d.error || "The computer could not start.", "err");
      }
    } catch {
      set({ computer: { ...emptyComputer, booting: false, error: "backend unreachable", msg: "backend unreachable" } });
      useUi.getState().toast("Could not reach the backend to start the computer.", "err");
    }
  },

  async computerStop() {
    try {
      await apiFetch("/computer/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: USER }),
      });
    } catch {
      /* ignore */
    }
    set({ computer: { ...emptyComputer } });
  },

  computerStatus() {
    if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "computer_status" }));
  },

  async refreshSysinfo() {
    try {
      const r = await apiFetch("/sysinfo");
      const d = await r.json();
      if (d && !d.ok && d.error) {
        // Remote computer unreachable — handled by computer_status elsewhere
        return;
      }
      set({ sysinfo: d });
    } catch {
      /* ignore */
    }
  },

  callTool(tool, args, timeout = 60) {
    return new Promise<ToolResult>((resolve) => {
      if (!ws || ws.readyState !== 1) {
        resolve({ output: "", error: "not connected", title: "", url: "", image: "", format: "" });
        return;
      }
      const id = `u${++reqSeq}`;
      pendingTools.set(id, {
        resolve,
        timer: setTimeout(() => {
          pendingTools.delete(id);
          resolve({ output: "", error: "timed out — the computer did not answer", title: "", url: "", image: "", format: "" });
        }, Math.min(timeout + 15, 120) * 1000),
      });
      ws.send(JSON.stringify({ type: "tool", id, tool, args, timeout }));
    });
  },

  async shell(cmd) {
    if (!cmd.trim()) return;
    set((s) => ({ terminal: [...s.terminal, { kind: "cmd", text: cmd }] }));
    const r = await get().callTool("shell", { command: cmd, timeout: 45 });
    if (r.error) set((s) => ({ terminal: [...s.terminal, { kind: "err", text: r.error }] }));
    else if (r.output) set((s) => ({ terminal: [...s.terminal, { kind: "out", text: r.output }] }));
  },

  clearTerminal() {
    set({ terminal: [] });
  },

  setVmPath(p) {
    set({ vmPath: p });
  },

  setWeb(title, url) {
    set({ webTitle: title, webUrl: url });
  },
}));

function scheduleReconnect() {
  if (retryTimer) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    useCore.getState().connect();
  }, 1500);
}