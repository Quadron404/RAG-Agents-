import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import {
  ArrowLeft,
  ChevronRight,
  Copy,
  FileText,
  Globe,
  Monitor,
  MoreHorizontal,
  Paperclip,
  Plus,
  Quote,
  Search,
  Star,
  Terminal,
  X,
} from "lucide-react";
import { BOTS, BOT_BY_ID, botForRole, useUi, type Message, type Step, type ToolCallState } from "../store";
import { useCore } from "../core";
import { useAgentStates } from "../lib/agents";
import { parseAgentField, relativeTime, timeStr } from "../lib/format";
import { Markdown } from "../lib/markdown";
import { speechSupported, startDictation } from "../lib/voice";
import { haptic } from "../lib/haptics";
import { IconButton } from "../components/ui";

/* ============================ utilities =================================== */

const stripTools = (t: string) => t.replace(/\[TOOL\s+\w+\s+\{.*?\}\]\s*/g, "");

const EMPTY_MSGS: Message[] = [];

function minifiedArgs(args: Record<string, unknown>): string {
  const keys = Object.keys(args);
  if (!keys.length) return "";
  const first = keys[0];
  const v = String(args[first] ?? "");
  return v.length > 40 ? v.slice(0, 40) + "…" : v;
}

/* hover/context menu items builder ------------------------------------------ */

function baseMessageMenu(m: Message, threadId: string, openMenu: ReturnType<typeof useUi.getState>["openMenu"], x: number, y: number) {
  const { toast } = useUi.getState();
  const items: { label: string; icon: string; danger?: boolean; onSelect: () => void }[] = [
    {
      label: "Copy",
      icon: "copy",
      onSelect: () => {
        navigator.clipboard?.writeText(m.content).catch(() => {});
        toast("Copied to clipboard", "ok");
        haptic("success");
      },
    },
    {
      label: "Reply",
      icon: "reply",
      onSelect: () => {
        useUi.getState().setReplyTo({ threadId, msgId: m.id, text: m.content });
        haptic("light");
      },
    },
  ];
  if (m.role === "user") {
    items.push({
      label: "Edit",
      icon: "edit",
      onSelect: () => {
        useCore.setState((s) => ({
          messages: {
            ...s.messages,
            [threadId]: (s.messages[threadId] || []).map((mm) =>
              mm.id === m.id ? { ...mm, editing: !mm.editing } : mm
            ),
          },
        }));
      },
    });
  }
  items.push(
    {
      label: "Share",
      icon: "share",
      onSelect: () => shareMessage(m.content),
    },
    {
      label: m.pinned ? "Unpin" : "Pin",
      icon: "pin",
      onSelect: () => {
        togglePin(threadId, m.id);
      },
    }
  );
  if (m.role === "user" && !m.pending) {
    items.push({
      label: "Remove",
      icon: "trash",
      danger: true,
      onSelect: () => {
        useCore.setState((s) => ({
          messages: { ...s.messages, [threadId]: (s.messages[threadId] || []).filter((mm) => mm.id !== m.id) },
        }));
      },
    });
  }
  openMenu(x, y, items);
}

async function shareMessage(text: string) {
  haptic("medium");
  try {
    if (navigator.share) {
      await navigator.share({ text });
      return;
    }
    await navigator.clipboard.writeText(text);
    useUi.getState().toast("Copied to clipboard", "ok");
  } catch {
    /* user cancelled */
  }
}

/* pins (conversation + message) ---------------------------------------------- */

function loadThreadPins(): Record<string, number[]> {
  try {
    return JSON.parse(localStorage.getItem("rag.pins") || "{}");
  } catch {
    return {};
  }
}

function togglePin(threadId: string, msgId: number) {
  const pins = loadThreadPins();
  const list = pins[threadId] || [];
  const next = list.includes(msgId) ? list.filter((x) => x !== msgId) : [...list, msgId];
  pins[threadId] = next;
  localStorage.setItem("rag.pins", JSON.stringify(pins));
  useUi.getState().toast(list.includes(msgId) ? "Removed from pinned" : "Pinned", "ok");
  haptic("success");
}

/* ============================ conversation list =========================== */

function Row({ t }: { t: { thread_id: string; title: string; created_at: number } }) {
  const active = useCore((s) => s.activeThreadId === t.thread_id);
  const open = useCore((s) => s.openThread);
  const toast = useUi((s) => s.toast);
  const setPinnedConv = useUi((s) => s.setPinnedConv);
  const archiveConv = useUi((s) => s.archiveConv);
  const openMenu = useUi((s) => s.openMenu);

  const menuItems: { label: string; icon?: string; danger?: boolean; onSelect: () => void }[] = [
    {
      label: active ? "Open" : "Open conversation",
      icon: "chat",
      onSelect: () => open(t.thread_id),
    },
    {
      label: "Pin to top",
      icon: "pin",
      onSelect: () => {
        setPinnedConv(t.thread_id);
        toast("Pinned conversation", "ok");
      },
    },
    {
      label: "Archive",
      icon: "trash",
      danger: true,
      onSelect: () => {
        archiveConv(t.thread_id);
        toast("Conversation archived", "info");
      },
    },
  ];

  return (
    <button
      className={`convrow${active ? " convrow--active" : ""}`}
      onClick={() => open(t.thread_id)}
      onContextMenu={(e) => {
        e.preventDefault();
        openMenu(e.clientX, e.clientY, menuItems);
      }}
    >
      <span className="convrow__mark">◇</span>
      <span className="convrow__meta">
        <span className="convrow__title">{t.title}</span>
        <span className="convrow__prev">{relativeTime(t.created_at)}</span>
      </span>
      <span className="convrow__time">{timeStr(t.created_at)}</span>
    </button>
  );
}

function ChatListPane() {
  const threadsAll = useCore((s) => s.threads);
  const setConvOpen = useUi((s) => s.setConvOpen);
  const newChat = useCore((s) => s.newChat);
  const archived = useUi((s) => s.archived);
  const pinnedConv = useUi((s) => s.pinnedConv);
  const activeThreadId = useCore((s) => s.activeThreadId);
  const run = useCore((s) => s.run);
  const status = useCore((s) => s.status);
  const bot = useUi((s) => s.bot);
  const setBot = useUi((s) => s.setBot);
  const [q, setQ] = useState("");

  const agentStates = useAgentStates(run && run.threadId === activeThreadId ? run : null);

  const threads = useMemo(() => {
    let list = threadsAll.filter((t) => !archived.includes(t.thread_id));
    if (q.trim()) {
      const needle = q.trim().toLowerCase();
      list = list.filter((t) => t.title.toLowerCase().includes(needle));
    }
    const pinned = list.find((t) => t.thread_id === pinnedConv);
    const rest = list.filter((t) => t.thread_id !== pinnedConv);
    return pinned ? [pinned, ...rest] : rest;
  }, [threadsAll, archived, pinnedConv, q]);

  const filteredAgents = q.trim()
    ? BOTS.filter((b) => b.name.toLowerCase().includes(q.trim().toLowerCase()))
    : BOTS;

  return (
    <div className="chatlist">
      <div className="chatlist__head">
        <span className="chatlist__title">Chats</span>
        <IconButton
          icon={Plus}
          className="icon-btn--mini"
          onClick={() => {
            haptic("medium");
            newChat();
          }}
          title="New conversation"
        />
      </div>
      <div className="search">
        <Search size={15} />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search…"
          aria-label="Search"
        />
      </div>
      <div className="chatlist__scroll">
        {filteredAgents.map((b) => {
          const st = agentStates[b.id];
          return (
            <button
              key={b.id}
              className={`cl-msg${bot === b.id ? " cl-msg--on" : ""}`}
              onClick={() => {
                haptic("medium");
                setBot(b.id);
              }}
            >
              <span className="msg__ava" style={{ background: b.gradient }} aria-hidden>
                {b.glyph}
                <span className={`dot dot--${st.status} msg-ava-dot`} />
              </span>
              <span className="cl-msg__meta">
                <span className="cl-msg__name">{b.name}</span>
                <span className="cl-msg__state">{st.label}</span>
              </span>
            </button>
          );
        })}

        {filteredAgents.length === BOTS.length && threads.length ? null : filteredAgents.length === BOTS.length ? (
          <div className="chatlist__empty">
            {status === "online" ? "No conversations yet." : "Connecting…"}
          </div>
        ) : null}
        {threads.map((t) => <Row key={t.thread_id} t={t} />)}
      </div>
      <span role="button" aria-label="Close chat list" className="convlist__mobile-close" onClick={() => setConvOpen(false)}>
        <ArrowLeft size={20} />
      </span>
    </div>
  );
}

export { ChatListPane };

/* ============================ message list ================================= */

function MessageBubble({ m, threadId }: { m: Message; threadId: string }) {
  const openMenu = useUi((s) => s.openMenu);
  const setReplyTo = useUi((s) => s.setReplyTo);
  const [editing, setEditing] = useState(false);
  const isUser = m.role === "user";
  const parsed = parseAgentField(m.agent);
  const bot = parsed.kind === "commander" ? BOT_BY_ID.commander : parsed.kind === "worker" ? botForRole(parsed.role) : null;

  const isEdited = (m.data && JSON.stringify(m.data).includes("edited")) || m.editing;

  return (
    <div
      className={`msg ${isUser ? "msg--user" : "msg--agent"}${parsed.kind === "commander" ? " msg--commander" : ""}`}
      onContextMenu={(e) => {
        e.preventDefault();
        haptic("light");
        baseMessageMenu(m, threadId, openMenu, e.clientX, e.clientY);
      }}
    >
      <div
        className="msg__ava"
        style={
          isUser
            ? { background: "var(--bg4)", color: "var(--text-2)" }
            : { background: bot?.gradient || "var(--bg3)", color: "#fff" }
        }
        aria-hidden
      >
        {isUser ? "You" : bot?.glyph || "◆"}
      </div>
      <div className="msg__body">
        <div className="msg__meta">
          <span className="msg__name">{isUser ? "You" : bot?.name || parsed.title}</span>
          {parsed.kind === "worker" ? <span className="msg__role">{parsed.title}</span> : null}
          {m.pending ? <span className="msg__role">sending…</span> : null}
          {isEdited ? <span className="msg__role">edited</span> : null}
          <span className="msg__time">{timeStr(m.created_at)}</span>
        </div>

        {editing ? (
          <EditBox
            m={m}
            threadId={threadId}
            onDone={() => setEditing(false)}
          />
        ) : (
          <div className="bubble">
            <div className="bubble__attach">
              {m.data && typeof m.data === "string" && m.data.includes("{") ? renderAttachChips(m.data) : null}
            </div>
            {m.content ? (
              <Markdown text={m.content} streaming={m.pending} />
            ) : (
              <span style={{ color: "var(--text-3)" }}>…</span>
            )}
          </div>
        )}

        <div className="msg__hover">
          <button className="act-btn" title="Copy" onClick={() => {
            navigator.clipboard?.writeText(m.content).catch(() => {});
            haptic("success");
            useUi.getState().toast("Copied", "ok");
          }}>
            <Copy size={15} />
          </button>
          <button
            className="act-btn"
            title="Reply"
            onClick={() => {
              haptic("light");
              setReplyTo({ threadId, msgId: m.id, text: m.content });
            }}
          >
            <Quote size={15} />
          </button>
          <button
            className="act-btn"
            title="More"
            onClick={() => baseMessageMenu(m, threadId, openMenu, window.innerWidth - 220, 120)}
          >
            <MoreHorizontal size={15} />
          </button>
        </div>
      </div>
    </div>
  );
}

function EditBox({ m, threadId, onDone }: { m: Message; threadId: string; onDone: () => void }) {
  const [v, setV] = useState(m.content);
  const save = () => {
    useCore.setState((s) => ({
      messages: {
        ...s.messages,
        [threadId]: (s.messages[threadId] || []).map((mm) =>
          mm.id === m.id ? { ...mm, content: v, data: '{"edited":true}' } : mm
        ),
      },
    }));
    haptic("medium");
    onDone();
  };
  return (
    <div className="bubble" style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 260 }}>
      <textarea
        className="composer__ta"
        autoFocus
        value={v}
        onChange={(e) => setV(e.target.value)}
        rows={3}
        style={{ background: "var(--bg1)", border: "1px solid var(--line)", borderRadius: 10, padding: "8px 10px", fontSize: 13.5 }}
      />
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button className="btn btn--ghost btn-s" style={{ padding: "6px 12px" }} onClick={onDone}>
          Cancel
        </button>
        <button className="btn btn--primary btn-s" style={{ padding: "6px 12px" }} onClick={save}>
          Save
        </button>
      </div>
    </div>
  );
}

function renderAttachChips(data: string) {
  try {
    const parsed = JSON.parse(data);
    if (Array.isArray(parsed.attachments)) {
      return parsed.attachments.map((a: { name?: string }, i: number) => (
        <span key={i} className="attach-chip">
          <FileText size={13} />
          {a.name}
        </span>
      ));
    }
  } catch {
    /* ignore */
  }
  return null;
}

/* ---- step cards & tool rows --------------------------------------------- */

const TOOL_META: Record<string, { icon: typeof Globe; color: string }> = {
  web_search: { icon: Globe, color: "var(--blue)" },
  fetch_url: { icon: Globe, color: "var(--blue)" },
  shell: { icon: Terminal, color: "var(--green)" },
  read_file: { icon: FileText, color: "var(--amber)" },
  write_file: { icon: FileText, color: "var(--amber)" },
  list_dir: { icon: FileText, color: "var(--amber)" },
  browser: { icon: Monitor, color: "var(--violet)" },
  browser_screenshot: { icon: Monitor, color: "var(--violet)" },
};

function ToolRow({ t }: { t: ToolCallState }) {
  const meta = TOOL_META[t.tool] || { icon: Terminal, color: "var(--text-2)" };
  const Icon = meta.icon;
  const [open, setOpen] = useState(false);
  return (
    <div className={`tool-row ${t.status === "running" ? "tool-row--busy" : ""} ${open ? "tool-row--open" : ""}`}>
      <span className="tool-row__ic" style={{ color: meta.color }}>
        <Icon size={15} />
      </span>
      <div className="tool-row__meta">
        <div className="tool-row__name">{t.tool}</div>
        {t.args ? <div className="tool-row__args">{minifiedArgs(t.args)}</div> : null}
      </div>
      <span className={`tool-row__state tool-row__state--${t.status === "running" ? "run" : t.status === "error" ? "err" : "ok"}`}>
        {t.status === "running" ? "…" : t.status === "error" ? "failed" : "done"}
      </span>
      {(t.output || t.error) && (t.tool !== "browser" || t.output) ? (
        <button
          className="act-btn"
          title={open ? "Collapse" : "Expand"}
          onClick={() => setOpen((o) => !o)}
          style={{ alignSelf: "center" }}
        >
          <ChevronRight size={15} style={{ transform: open ? "rotate(90deg)" : undefined, transition: "transform .15s" }} />
        </button>
      ) : null}
      {(t.output || t.error) && (
        <div className="tool-row__out">
          {t.error ? <span style={{ color: "var(--red)" }}>{t.error}</span> : null}
          {t.output ? t.output : null}
        </div>
      )}
    </div>
  );
}

function StepCard({ step, busy }: { step: Step; busy: boolean }) {
  const [open, setOpen] = useState(true);
  const bot = botForRole(step.role);
  const progress =
    step.status === "done" ? 100 : step.status === "working" ? 55 : 8;
  const hasText = step.delta.trim().length > 0;

  return (
    <div className={`step-card ${open ? "step-card--open" : ""} ${busy ? "step-card--busy" : ""}`}>
      <button className="step-card__head" onClick={() => {
        setOpen((o) => !o);
        haptic("light");
      }}>
        <span className="step-card__art" style={{ background: bot.gradient }}>
          {bot.glyph}
          {step.status === "working" ? <span className="dot dot--working msg-ava-dot" /> : null}
        </span>
        <span className="step-card__heading">
          <span className="step-card__title">{step.title}</span>
          <span className="step-card__agent">
            {busy ? `${bot.name} is working…` : step.status === "done" ? "completed" : "waiting"}
          </span>
        </span>
        <ChevronRight className="step-card__chev" size={17} />
      </button>
      <div className="step-card__prog">
        <i className={step.status === "working" ? "work" : ""} style={{ width: `${progress}%` }} />
      </div>
      <div className="step-card__body">
        {step.tools.length ? (
          <div className="tool-list">
            {step.tools.map((t) => (
              <ToolRow key={t.id} t={t} />
            ))}
          </div>
        ) : null}
        {busy && !hasText && !step.tools.length ? (
          <div className="think">
            <span className="d" /> <span className="d" /> <span className="d" /> thinking
          </div>
        ) : null}
        {hasText ? (
          <Markdown text={stripTools(step.delta)} streaming={busy} className="step-card__thought" />
        ) : null}
        {step.report ? (
          <div className="step-card__report">
            <h5>Report</h5>
            <Markdown text={step.report} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

/* ---- streaming run banner ------------------------------------------------- */

function RunBanner() {
  const run = useCore((s) => s.run);
  const activeThreadId = useCore((s) => s.activeThreadId);
  if (!run || run.threadId !== activeThreadId || run.phase === "done") return null;
  const label =
    run.phase === "planning"
      ? "Commander is planning the approach…"
      : run.phase === "synthesizing"
        ? "Commander is writing the final answer…"
        : "Workers are running on the computer…";
  return (
    <div className="runbanner" role="status" aria-live="polite">
      <span className="dot dot--working" />
      <span className="runbanner__text">{label}</span>
      <span style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--text-3)", fontWeight: 700 }}>
        {run.steps.filter((s) => s.status === "done").length}/{run.steps.length || 1}
        {run.steps.length ? " steps" : ""}
      </span>
    </div>
  );
}

/* ---- empty state ---------------------------------------------------------- */

function EmptyChat() {
  const send = useCore((s) => s.send);
  const looks = useMemo(() => ["Compare the top 3 Linux distros", "Summarize any PDF I attach", "Search my files for a project", "Open a page and read it back"], []);
  return (
    <div className="empty">
      <div className="empty__orb">
        <Quote size={40} />
      </div>
      <div className="empty__title">Talk to your workforce</div>
      <div className="empty__sub">
        Ask the Commander anything, or direct a specialist directly. Agents coordinate,
        use the computer, and report back in one calm conversation.
      </div>
      <div className="suggests">
        {looks.map((s) => (
          <button
            key={s}
            className="suggest"
            onClick={() => {
              haptic("medium");
              send(s);
            }}
          >
            <Star size={15} />
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---- the thread ------------------------------------------------------------------ */

function MessageList() {
  const activeThreadId = useCore((s) => s.activeThreadId);
  const messages = useCore((s) => (s.activeThreadId ? s.messages[s.activeThreadId] : EMPTY_MSGS));
  const run = useCore((s) => s.run);
  const liveRun = run && run.threadId === activeThreadId && run.phase !== "done" ? run : null;

  const items = useMemo(() => {
    return (messages || []).slice();
  }, [messages]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickBottom = useRef(true);

  const scrollToBottom = useCallback((smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
  };

  useEffect(() => {
    if (stickBottom.current) scrollToBottom();
  }, [items, liveRun, scrollToBottom]);

  if (!activeThreadId) return <EmptyChat />;

  return (
    <div className="thread__scroll" ref={scrollRef} onScroll={onScroll}>
      <div className="thread__inner">
        {items.map((m) => (
          <MessageBubble key={m.id} m={m} threadId={activeThreadId} />
        ))}
        {liveRun ? <RunBanner /> : null}
        {liveRun
          ? liveRun.steps.map((s) => <StepCard key={s.id} step={s} busy={s.status === "working"} />)
          : null}
        {liveRun && liveRun.commanderDelta ? (
          <div className="msg msg--agent msg--commander">
            <div className="msg__ava" style={{ background: BOT_BY_ID.commander.gradient, color: "#fff" }}>
              ◇
            </div>
            <div className="msg__body">
              <div className="msg__meta">
                <span className="msg__name">{BOT_BY_ID.commander.name}</span>
                <span className="msg__time">{timeStr()}</span>
              </div>
              <div className="bubble">
                <Markdown text={liveRun.commanderDelta} streaming />
                <span className="cursor" />
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/* ============================ composer ====================================== */

interface Attach {
  name: string;
  kind: "image" | "file";
  data?: string;
  size: number;
}

function Composer() {
  const [text, setText] = useState("");
  const [attaches, setAttaches] = useState<Attach[]>([]);
  const [focus, setFocus] = useState(false);
  const [listening, setListening] = useState(false);
  const status = useCore((s) => s.status);
  const send = useCore((s) => s.send);
  const stopRun = useCore((s) => s.stopRun);
  const run = useCore((s) => s.run);
  const activeThreadId = useCore((s) => s.activeThreadId);
  const replyTo = useUi((s) => s.replyTo);
  const clearReplyTo = useUi((s) => s.clearReplyTo);
  const bot = useUi((s) => s.bot);
  const toast = useUi((s) => s.toast);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const dict = useRef<ReturnType<typeof startDictation> | null>(null);

  const busy = run && run.threadId === activeThreadId && run.phase !== "done" && run.phase !== "idle";

  const autoGrow = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 150) + "px";
  }, []);

  useEffect(autoGrow, [text, autoGrow]);

  const commit = () => {
    const base = text.trim();
    if (busy || status !== "online") return;
    if (!base && !attaches.length) return;
    const attachLine = attaches.length
      ? `\n\n[attached: ${attaches.map((a) => a.name).join(", ")}]`
      : "";
    const finalText = base + attachLine;
    haptic("heavy");
    send(finalText);
    setText("");
    setAttaches([]);
    autoGrow();
    if (replyTo) clearReplyTo();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      commit();
    }
  };

  const toggleVoice = () => {
    if (listening) {
      dict.current?.stop();
      setListening(false);
      return;
    }
    if (!speechSupported()) {
      toast("Voice input isn't supported here", "info");
      return;
    }
    haptic("medium");
    setListening(true);
    dict.current = startDictation({
      onInterim: (t) => setText((prev) => (prev.endsWith(t) ? prev : prev ? `${prev} ${t}` : t)),
      onFinal: (t) => setText((prev) => (prev.endsWith(t) ? prev : prev ? `${prev} ${t}` : t)),
      onEnd: () => setListening(false),
      onError: (msg) => toast(msg, "err"),
    });
  };

  const addFiles = (list: FileList | null) => {
    if (!list) return;
    Array.from(list).slice(0, 6).forEach((f) => {
      const img = f.type.startsWith("image/");
      if (img && f.size < 3 * 1024 * 1024) {
        const reader = new FileReader();
        reader.onload = () => {
          setAttaches((a) => [...a, { name: f.name, kind: "image", data: String(reader.result), size: f.size }]);
        };
        reader.readAsDataURL(f);
      } else {
        setAttaches((a) => [...a, { name: f.name, kind: "file", size: f.size }]);
      }
    });
    haptic("light");
  };

  return (
    <div className="composer-shell">
      <div className={`composer${focus ? " composer--focus" : ""}`}>
        {replyTo ? (
          <div className="composer__replychip">
            <Quote size={13} />
            <span className="composer__replytext">{replyTo.text.slice(0, 80)}</span>
            <button className="fx-chip__del" onClick={clearReplyTo} aria-label="Clear reply">
              <X size={12} />
            </button>
          </div>
        ) : null}
        {attaches.length ? (
          <div className="composer__attach-row">
            {attaches.map((a, i) => (
              <span className="fx-chip" key={i}>
                {a.kind === "image" && a.data ? <img src={a.data} alt="" /> : <FileText size={15} />}
                <span className="mono" style={{ maxWidth: 140, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {a.name}
                </span>
                <button
                  className="fx-chip__del"
                  onClick={() => setAttaches((l) => l.filter((_, j) => j !== i))}
                  aria-label={`Remove ${a.name}`}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
          </div>
        ) : null}

        <div className="composer__row">
          <textarea
            ref={taRef}
            className="composer__ta"
            rows={1}
            value={text}
            placeholder={`Message ${BOT_BY_ID[bot].name}…`}
            onChange={(e) => setText(e.target.value)}
            onFocus={() => setFocus(true)}
            onBlur={() => setFocus(false)}
            onKeyDown={onKey}
            aria-label="Message"
          />
          <input ref={fileRef} type="file" multiple hidden accept="image/*,.pdf,.txt,.md,.csv,.json,.py,.js,.ts,.sh,.docx" onChange={(e) => addFiles(e.target.files)} />
          {speechSupported() ? (
            <button
              className={`composer__btn${listening ? " composer__btn--voice-active" : ""}`}
              onClick={toggleVoice}
              title={listening ? "Stop voice input" : "Voice input"}
              aria-label={listening ? "Stop voice input" : "Voice input"}
            >
              {listening ? (
                <span className="voice-meter">
                  <i /><i /><i /><i /><i /><i />
                </span>
              ) : (
                <MicIcon />
              )}
            </button>
          ) : null}
          <button
            className="composer__btn"
            onClick={() => fileRef.current?.click()}
            title="Attach"
            aria-label="Attach"
          >
            <Paperclip size={18} />
          </button>
          {busy ? (
            <button className="composer__send composer__stop" onClick={stopRun} title="Stop" aria-label="Stop">
              <span className="stop-ic" />
            </button>
          ) : (
            <button
              className="composer__send"
              onClick={commit}
              disabled={!text.trim() || status !== "online"}
              aria-label="Send"
            >
              <SendArrow />
            </button>
          )}
        </div>
        <div className="composer__hint">
          <span className="dot dot--online" />
          <span>Commander plans · workers run on</span>
          <b>your computer</b>
          <span>· Enter to send, Shift+Enter for a new line</span>
        </div>
      </div>
    </div>
  );
}

function MicIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
      <line x1="12" y1="18" x2="12" y2="22" />
    </svg>
  );
}

function SendArrow() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor" stroke="none">
      <path d="M3.4 20.4l17.7-7.6a1 1 0 0 0 0-1.8L3.4 3.4a1 1 0 0 0-1.4 1.1l1.7 6.2a.3.3 0 0 1-.2.4l5.4 1a.3.3 0 0 1 0 .6l-5.4 1a.3.3 0 0 1 .2.4l-1.7 6.2a1 1 0 0 0 1.4 1.1z" />
    </svg>
  );
}

/* ============================ chat view ===================================== */

export function ChatView() {
  const activeThreadId = useCore((s) => s.activeThreadId);
  const run = useCore((s) => s.run);
  const setConvOpen = useUi((s) => s.setConvOpen);
  const bot = useUi((s) => s.bot);
  const setBot = useUi((s) => s.setBot);

  const agentStates = useAgentStates(run && run.threadId === activeThreadId ? run : null);
  const st = agentStates[bot];
  const running = !!run && run.threadId === activeThreadId && run.phase !== "done" && run.phase !== "idle";

  const cycleBot = () => {
    const order = ["commander", "researcher", "engineer", "navigator"] as const;
    const i = order.indexOf(bot);
    const next = order[(i + 1) % order.length];
    setBot(next);
    haptic("light");
  };

  return (
    <div className="thread">
      <div className="thread__header">
        <button className="thread__back" onClick={() => setConvOpen(true)} aria-label="Open conversations">
          <ArrowLeft size={19} />
        </button>
        <button className="thread__ava" onClick={cycleBot} title="Switch agent" aria-label="Switch agent">
          <span className="msg__ava" style={{ background: BOT_BY_ID[bot].gradient }} aria-hidden>
            {BOT_BY_ID[bot].glyph}
          </span>
        </button>
        <button className="thread__hmeta" onClick={cycleBot} title="Switch agent">
          <div className="thread__hname">{BOT_BY_ID[bot].name}</div>
          <div className="thread__hstate">
            {running ? (run && run.phase !== "planning" ? "working on it…" : "planning…") : st.label}
          </div>
        </button>
      </div>
      <MessageList />
      <Composer />
    </div>
  );
}