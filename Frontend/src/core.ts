import { create } from "zustand";
import { useUi, loadLastActiveThread, saveLastActiveThread, type ComputerState, ConnectionStatus, Message, Run, Step, SysInfo, TerminalLine, Thread, ToolCallState } from "./store";
import { currentCsrf, currentUserId } from "./auth";

/* ============================================================================
   Backend addressing — same-origin in dev (Vite proxy) and production
   (the Worker serves the build and proxies the API); override with
   VITE_BACKEND for direct access.
   ========================================================================== */

const BACKEND: string = (import.meta.env.VITE_BACKEND as string) || "";

export const api = (path: string): string => (BACKEND ? BACKEND + path : path);

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * fetch() for API calls, with the session cookie attached.
 *
 * The app is served from the same origin as the API, so the HttpOnly session
 * cookie is what authorises every call; "include" is what makes the browser
 * attach it.  The edge session is cookie-based, which means a state-changing
 * call must also echo the per-session CSRF token (read from /auth/session into
 * the auth store).  Routing every call through one helper is what guarantees no
 * endpoint quietly ends up without a credential or a CSRF header.
 */
export const apiFetch = (path: string, init: RequestInit = {}): Promise<Response> => {
  const method = (init.method || "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!SAFE_METHODS.has(method)) {
    const csrf = currentCsrf();
    if (csrf) headers.set("x-rag-csrf", csrf);
  }
  return fetch(api(path), { credentials: "include", ...init, headers });
};

export function wsUrl(path: string): string {
  if (BACKEND) {
    return BACKEND.replace(/^http:/, "ws:").replace(/^https:/, "wss:") + path;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

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
let chatAbort: AbortController | null = null;

/* ============================================================================
   Core store
   ========================================================================== */

interface RunEvents {
  type:
    | "plan"
    | "worker_start"
    | "delta"
    | "tool_start"
    | "tool_output"
    | "tool_result"
    | "synthesize"
    | "done"
    | "error";
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
  restoreActiveThread: (threads: Thread[]) => Promise<void>;
  newChat: () => Promise<void>;
  generateHistory: (threadId: string) => Promise<void>;
  generateHistoryStatus: "idle" | "processing" | "success" | "error";
  generateHistoryError: string | null;
  generateHistorySource: { role: string; content: string; seq: number }[] | null;
  generateHistorySummary: string | null;
  setGenerateHistoryStatus: (s: "idle" | "processing" | "success" | "error", e?: string | null) => void;
  resetAccount: () => Promise<boolean>;
  openThread: (id: string) => Promise<void>;
  loadMessages: (id: string) => Promise<void>;
  //: Returns false when the message was not dispatched (not connected, or a run
  //: is already in flight) so the caller can keep the draft and say why.
  send: (text: string) => boolean;
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

async function finalize() {
  if (finalizeTimer) clearTimeout(finalizeTimer);
  finalizeTimer = null;
  const core = useCore.getState();
  const run = core.run;
  if (!run || run.cancel || run.phase === "done") return;

  // Load the transcript FIRST, while the streamed view is still visible.  The
  // backend only emits `done` after it has written the assistant turn to disk,
  // so by the time we get here the real message is already in the server
  // transcript.  Loading before hiding the streamed bubble is what stops a
  // finished answer from vanishing: the old code flipped the run to "done"
  // (which unmounts the streamed view) on a fixed timer off `synthesize`, so a
  // synthesis that took longer than the timer raced the write and the answer
  // disappeared until the next manual reload.
  if (core.activeThreadId) {
    try {
      await core.loadMessages(core.activeThreadId);
    } catch {
      /* keep whatever we had */
    }
  }

  const latest = useCore.getState().run;
  if (!latest || latest.cancel || latest.phase === "done") return;
  const steps = latest.steps.map((s) => ({ ...s, status: "done" as const }));
  useCore.setState({ run: { ...latest, phase: "done", steps } });
  useCore.getState().refreshThreads();
}

function scheduleFinalize(delayMs = 120000) {
  // A safety net only.  The real, authoritative close is the backend's `done`
  // event; this just guarantees a run can never hang the composer forever if a
  // socket drops mid-turn and `done` never arrives.
  if (finalizeTimer) clearTimeout(finalizeTimer);
  finalizeTimer = setTimeout(() => {
    void finalize();
  }, delayMs);
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
      useCore.setState({ status: "online", userId: d.user_id || currentUserId() });
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
      // The backend sends a terminal `done` once the turn is persisted on disk;
      // this 120s timer is only a long-stop safety net in case that event
      // never arrives (e.g. the socket dropped mid-turn).
      scheduleFinalize(120000);
      break;
    case "done":
      // Authoritative end-of-turn: the assistant message is already written, so
      // close the run and swap the streamed view for the real transcript.
      void finalize();
      break;
    case "error": {
      // A turn that died on a model call still has to end here.  The old
      // behaviour surfaced this only as a transient toast, so the run stayed in
      // "planning"/"working" and the composer refused every further message
      // forever.  Finalising marks the run done and reloads the transcript,
      // where the backend has already recorded the failure as an assistant turn.
      const message = String(ev.error || "The AI request failed.");
      useUi.getState().toast(message, "err");
      void finalize();
      break;
    }
  }
}

/* --------------------------------------------------------------------------
   Worker-native chat over SSE
   ----------------------------------------------------------------------- */

async function createThreadAndStream(text: string, localId: string): Promise<void> {
  try {
    const response = await apiFetch("/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "New chat" }),
    });
    const data = await response.json();
    if (!response.ok || !data.thread_id) {
      throw new Error(String(data.error || "Could not create a conversation."));
    }
    const threadId = String(data.thread_id);
    useCore.setState((s) => {
      const pending = s.messages["__new__"] || [];
      const messages = { ...s.messages, [threadId]: pending.map((m) => ({ ...m, thread_id: threadId })) };
      delete messages["__new__"];
      return {
        activeThreadId: threadId,
        messages,
        run: s.run
          ? { ...s.run, threadId }
          : {
              threadId,
              phase: "working" as const,
              plan: null,
              steps: [],
              commanderDelta: "",
              startedAt: Date.now(),
              cancel: false,
            },
      };
    });
    useCore.getState().refreshThreads();
    await streamChat(threadId, text, localId);
  } catch (error) {
    removePending("__new__", localId);
    patchRun({ phase: "done", cancel: true });
    useUi.getState().toast(String((error as Error).message || "Could not start the conversation."), "err");
  }
}

function removePending(threadId: string, localId: string): void {
  useCore.setState((s) => ({
    messages: {
      ...s.messages,
      [threadId]: (s.messages[threadId] || []).filter((m) => m.id !== localId),
    },
  }));
}

function upsertMessage(threadId: string, message: Message): void {
  useCore.setState((s) => {
    const list = [...(s.messages[threadId] || [])];
    const index = list.findIndex((m) => m.id === message.id);
    if (index >= 0) list[index] = { ...list[index], ...message, pending: false };
    else list.push(message);
    return { messages: { ...s.messages, [threadId]: list } };
  });
}

function handleChatEvent(type: string, payload: Record<string, unknown>, threadId: string, localId: string): void {
  switch (type) {
    case "meta": {
      const user = payload.user_message as Message | null | undefined;
      if (user && user.id) {
        useCore.setState((s) => {
          const list = [...(s.messages[threadId] || [])];
          const index = list.findIndex((m) => m.id === localId);
          if (index >= 0) list[index] = { ...user, pending: false };
          else if (!list.some((m) => m.id === user.id)) list.push(user);
          return { messages: { ...s.messages, [threadId]: list } };
        });
      } else {
        useCore.setState((s) => ({
          messages: {
            ...s.messages,
            [threadId]: (s.messages[threadId] || []).map((m) =>
              m.id === localId ? { ...m, pending: false } : m,
            ),
          },
        }));
      }
      const current = useCore.getState().run;
      useCore.setState({
        run: current
          ? { ...current, threadId, phase: "working" }
          : { ...setRun(threadId), phase: "working" },
      });
      break;
    }
    case "delta": {
      const text = String(payload.text || "");
      if (!text) break;
      const current = useCore.getState().run || setRun(threadId);
      useCore.setState({
        run: { ...current, threadId, phase: "working", commanderDelta: current.commanderDelta + text },
      });
      break;
    }
    case "reset": {
      const current = useCore.getState().run;
      if (current) useCore.setState({ run: { ...current, commanderDelta: "" } });
      break;
    }
    case "tool": {
      const current = useCore.getState().run || setRun(threadId);
      const toolId = String(payload.id || "");
      const name = String(payload.name || "tool");
      const status = String(payload.status || "start");
      const count = Number(payload.result_count || 0);
      const query = String(payload.query || "");
      const existing = current.steps.find((s) => s.id === "chief");
      const done = status !== "start";
      const tool: ToolCallState = {
        id: toolId,
        tool: name,
        args: query ? { query } : {},
        status: done ? (payload.error ? "error" : "ok") : "running",
        output: done && count ? `${count} matching ${count === 1 ? "summary" : "summaries"}` : "",
        error: String(payload.error || ""),
        open: false,
      };
      const steps = existing
        ? current.steps.map((s) =>
            s.id === "chief"
              ? {
                  ...s,
                  status: "working" as const,
                  tools: s.tools.some((t) => t.id === toolId)
                    ? s.tools.map((t) => (t.id === toolId ? { ...t, ...tool } : t))
                    : [...s.tools, tool],
                }
              : s,
          )
        : [
            {
              id: "chief",
              agent: "chief",
              title: "Chief",
              role: "commander",
              status: "working" as const,
              tools: [tool],
              delta: "",
              report: "",
            },
          ];
      useCore.setState({ run: { ...current, threadId, phase: "working", steps } });
      break;
    }
    case "done": {
      const message = payload.message as Message | null | undefined;
      if (message && message.id) upsertMessage(threadId, message);
      const current = useCore.getState().run;
      if (current) useCore.setState({ run: { ...current, phase: "done", commanderDelta: "" } });
      void useCore.getState().loadMessages(threadId);
      void useCore.getState().refreshThreads();
      break;
    }
    case "error": {
      const message = payload.message as Message | null | undefined;
      if (message && message.id) upsertMessage(threadId, message);
      const current = useCore.getState().run;
      if (current) useCore.setState({ run: { ...current, phase: "done" } });
      useUi.getState().toast(String(payload.error || "The AI request failed."), "err");
      void useCore.getState().loadMessages(threadId);
      break;
    }
  }
}

async function streamChat(threadId: string, text: string, localId: string): Promise<void> {
  const requestId = `req-${crypto.randomUUID()}`;
  const controller = new AbortController();
  chatAbort?.abort(new Error("superseded"));
  chatAbort = controller;
  try {
    const response = await apiFetch(`/threads/${encodeURIComponent(threadId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, request_id: requestId }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(String((data as { error?: string }).error || `HTTP ${response.status}`));
    }
    if (!response.body) throw new Error("The response stream was empty.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let settled = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
      let split = buffer.indexOf("\n\n");
      while (split >= 0) {
        const block = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        let event = "message";
        let data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (data) {
          try {
            handleChatEvent(event, JSON.parse(data) as Record<string, unknown>, threadId, localId);
            if (event === "done" || event === "error") settled = true;
          } catch {
            /* ignore a malformed event */
          }
        }
        split = buffer.indexOf("\n\n");
      }
    }
    if (!settled && !controller.signal.aborted) {
      removePending(threadId, localId);
      patchRun({ phase: "done", cancel: true });
      useUi.getState().toast("The response ended before Chief finished.", "err");
      await useCore.getState().loadMessages(threadId);
    }
  } catch (error) {
    if (controller.signal.aborted) return;
    removePending(threadId, localId);
    patchRun({ phase: "done", cancel: true });
    useUi.getState().toast(String((error as Error).message || "The AI request failed."), "err");
    void useCore.getState().loadMessages(threadId);
  } finally {
    if (chatAbort === controller) chatAbort = null;
  }
}

/* ============================================================================
   Store
   ========================================================================== */

export const useCore = create<CoreState>((set, get) => ({
  status: "connecting",
  userId: currentUserId(),
  threads: [],
  generateHistoryStatus: "idle",
  generateHistoryError: null,
  generateHistorySource: null,
  generateHistorySummary: null,
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
    // Chat is served by the Worker and stays available even when the Codespace
    // socket is down.  The socket is only for computer controls and live screen.
    set({ status: "online" });
    // Load the user's existing conversations immediately so the sidebar is
    // populated even before the WebSocket delivers its first event.
    void get().refreshThreads();
    try {
      ws = new WebSocket(wsUrl(`/ws/${currentUserId()}`));
    } catch {
      scheduleReconnect();
      return;
    }
    ws.onopen = () => {
      get().computerStatus();
    };
    ws.onclose = () => {
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
      const r = await apiFetch(`/threads?user_id=${currentUserId()}`);
      const d = await r.json();
      set({ threads: d.threads || [] });
      if (!get().activeThreadId) {
        await get().restoreActiveThread(d.threads || []);
      }
    } catch {
      /* retry on next connect */
    }
  },

  /** Restore the user's last active channel without creating one.
   *
   * The backend's /session/active-thread is the source of truth.  A localStorage
   * hint is used only to restore the UI instantly while the server round trip is
   * in flight, and it is always verified against the server's thread list so a
   * stale or tampered id can never resurrect a channel the user no longer has.
   * When nothing is set and threads exist, the most recent one is opened -- but
   * never a brand-new channel: creation is an explicit user action (newChat).
   */
  async restoreActiveThread(threads: Thread[]) {
    // 1. Server source of truth.
    try {
      const r = await apiFetch("/session/active-thread");
      const d = (await r.json()) as { active_thread_id?: string | null };
      const serverThread = d.active_thread_id || null;
      if (serverThread && threads.some((t) => t.thread_id === serverThread)) {
        set({ activeThreadId: serverThread });
        await get().loadMessages(serverThread);
        return;
      }
    } catch {
      /* fall through to the localStorage hint below */
    }

    // 2. Browser-storage hint, verified against the server's thread list.
    const hint = loadLastActiveThread();
    if (hint && threads.some((t) => t.thread_id === hint)) {
      set({ activeThreadId: hint });
      await get().loadMessages(hint);
      return;
    }

    // 3. Nothing restored: fall back to the most recent thread, if any.
    if (!get().activeThreadId && threads.length) {
      get().openThread(threads[0].thread_id);
    }
  },

    async newChat() {
      try {
        const r = await apiFetch("/threads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: currentUserId(), title: "New conversation" }),
        });
        const d = await r.json();
        set({ activeThreadId: d.thread_id, run: null, screen: null });
        saveLastActiveThread(d.thread_id);
        await get().refreshThreads();
        useUi.getState().setView("chat");
      } catch {
        useUi.getState().toast("Could not create a new conversation.", "err");
      }
    },

    async generateHistory(threadId: string) {
      if (!threadId) return;
      set({ generateHistoryStatus: "processing", generateHistoryError: null });
      try {
        const r = await apiFetch(`/threads/${threadId}/generate-history`, { method: "POST" });
        const d = await r.json();
        if (!r.ok) {
          set({ generateHistoryStatus: "error", generateHistoryError: d.error || "Generation failed", generateHistorySource: null });
          useUi.getState().toast(d.error || "Could not generate history.", "err");
          return;
        }
        set({ generateHistoryStatus: "success", generateHistoryError: null, generateHistorySource: d.sourceMessages ?? null, generateHistorySummary: d.summary ?? null });
        useUi.getState().toast("History generated successfully.", "ok");
      } catch (e) {
        set({ generateHistoryStatus: "error", generateHistoryError: String(e) });
        useUi.getState().toast("Could not generate history.", "err");
      }
    },

    setGenerateHistoryStatus(s: "idle" | "processing" | "success" | "error", e?: string | null) {
      set({ generateHistoryStatus: s, generateHistoryError: e ?? null });
    },

    async resetAccount() {
      try {
        const r = await apiFetch("/auth/settings/reset", { method: "POST" });
        const d = await r.json();
        if (!r.ok) {
          useUi.getState().toast(d.error || "Reset failed.", "err");
          return false;
        }
        if (d.indexErrors && d.indexErrors.length) {
          useUi.getState().toast("Reset complete. Some indexed documents could not be removed immediately and will be retried.", "ok");
        } else {
          useUi.getState().toast("Your account data has been reset.", "ok");
        }
      } catch (e) {
        useUi.getState().toast(String(e || "Reset failed."), "err");
        return false;
      }

      // Clear every piece of frontend-cached user data so nothing survives a
      // reload.  The server already deleted the canonical records.
      set({
        threads: [],
        messages: {},
        activeThreadId: null,
        run: null,
        screen: null,
        generateHistoryStatus: "idle",
        generateHistoryError: null,
        generateHistorySource: null,
        generateHistorySummary: null,
      });
      localStorage.removeItem("rag.prefs.v1");
      localStorage.removeItem("rag.ignores.v1");
      localStorage.removeItem("rag.pins");
      localStorage.removeItem("rag.active-thread.v1");
      window.dispatchEvent(new StorageEvent("storage"));
      location.reload();
      return true;
    },

  async openThread(id) {
    set({ activeThreadId: id });
    saveLastActiveThread(id);
    useUi.getState().setView("chat");
    await get().loadMessages(id);
    // Persist the active channel server-side so a refresh or reopen on another
    // device restores the same conversation.  Ownership is enforced by the
    // backend; a 404 here just means the thread doesn't belong to this user.
    try {
      await apiFetch(`/threads/${encodeURIComponent(id)}/activate`, {
        method: "POST",
      });
    } catch {
      /* the hint in localStorage still allows a fast local restore next time */
    }
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
    const { activeThreadId, run } = get();
    if (!text.trim()) return false;
    const busy =
      run &&
      run.phase !== "done" &&
      run.phase !== "idle" &&
      (!activeThreadId || run.threadId === activeThreadId);
    if (busy) {
      useUi.getState().toast("Chief is already working on your request.", "info");
      return false;
    }
    const localId = `local-${crypto.randomUUID()}`;
    const target = activeThreadId || "__new__";
    pushMsg(target, {
      id: localId,
      thread_id: target,
      role: "user",
      agent: "user",
      content: text,
      data: "{}",
      seq: 0,
      created_at: Date.now() / 1000,
      pending: true,
    });
    setRun(target);
    if (activeThreadId) {
      void streamChat(activeThreadId, text, localId);
    } else {
      void createThreadAndStream(text, localId);
    }
    return true;
  },

  stopRun() {
    const { run } = get();
    if (run) patchRun({ cancel: true, phase: "done" });
    chatAbort?.abort(new Error("stopped by user"));
    chatAbort = null;
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
        body: JSON.stringify({ user_id: currentUserId() }),
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
        body: JSON.stringify({ user_id: currentUserId() }),
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