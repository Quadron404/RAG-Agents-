import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  Bot,
  ChevronDown,
  Copy,
  Cpu,
  Loader2,
  Monitor,
  MousePointerClick,
  Send,
  Terminal,
  User,
  X,
} from "lucide-react";
import {
  fetchComputerTrace,
  type ComputerTrace,
  type ComputerTurn,
} from "../lib/screen";

/**
 * The Computer Chat: the computer-use loop as a conversation you can watch.
 *
 * The point is to answer "what is the AI actually seeing and saying, right
 * now" without opening a log file.  So this is a timeline, not a table: the
 * task, then for every request the input the model was handed, the bytes it
 * sent back, what the parser made of them, what the machine did, and the frame
 * that came after.
 *
 * Three things it refuses to do, because each one destroys the only evidence
 * there is when a run goes wrong:
 *
 *  1. It never rewrites a model reply.  Not trimmed, not prettified, not
 *     summarised, not replaced with "thinking".  Prose where JSON was wanted
 *     looks exactly like a working reply unless you can see the text.
 *  2. It never draws a picture that is not the frame the model was sent.  The
 *     only images here are the base64 from the trace, which is the same bytes
 *     that went into the request.  No VNC grab, no render of the RAG Agents
 *     window, no stand-in -- a picture of a different screen is worse than no
 *     picture, because it answers the question wrongly rather than not at all.
 *  3. It never claims a click landed.  It shows what was asked for and what X
 *     reported back, and when those two differ that is the finding.
 */
export default function ComputerChat({ taskId }: { taskId: string }) {
  const [trace, setTrace] = useState<ComputerTrace | null>(null);
  const [error, setError] = useState("");
  const [zoom, setZoom] = useState<{ src: string; label: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const load = useCallback(async () => {
    try {
      setTrace(await fetchComputerTrace(taskId));
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [taskId]);

  // Polled, so the panel fills in while the run is still going.  A trace read
  // once at the end misses the turn that went wrong, which is usually not the
  // last one.
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 1500);
    return () => window.clearInterval(timer);
  }, [load]);

  // Follow the newest turn, but only while the user is already at the bottom.
  // Yanking the view down mid-scroll while they are reading an earlier reply is
  // the fastest way to make a live log unusable.
  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
  };

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [trace]);

  useEffect(() => {
    if (!zoom) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setZoom(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoom]);

  const copy = async () => {
    if (!trace) return;
    await navigator.clipboard.writeText(JSON.stringify(trace, null, 2));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <aside className="ccchat" aria-label="Computer chat: what the AI receives and sends">
      <header className="ccchat__head">
        <div className="ccchat__title">
          <Bot size={15} aria-hidden="true" />
          <span>Computer Chat</span>
          <span className="ccchat__count">
            {trace ? `${trace.turns.length} ${trace.turns.length === 1 ? "request" : "requests"}` : "—"}
          </span>
        </div>
        <div className="ccchat__head-actions">
          <button type="button" onClick={() => void copy()} disabled={!trace} title="Copy the whole trace as JSON">
            <Copy size={13} aria-hidden="true" />
            {copied ? "Copied" : "Copy trace"}
          </button>
        </div>
      </header>

      <div className="ccchat__scroll" ref={scroller} onScroll={onScroll}>
        {error && <p className="ccchat__error">{error}</p>}
        {!trace && !error && (
          <p className="ccchat__empty">
            <Loader2 size={15} className="ccchat__spin" aria-hidden="true" /> Reading the run…
          </p>
        )}

        {trace && (
          <>
            <div className="ccchat__meta">
              <span className={`ccchat__status ccchat__status--${trace.status}`}>{trace.status}</span>
              <span title="The model that answered every request below">{trace.model || "—"}</span>
            </div>

            {/* The task, exactly as it was submitted.  It is also repeated in
                every request's history, but the conversation needs it stated
                once at the top to read as a conversation. */}
            <Bubble side="user" icon={<User size={13} aria-hidden="true" />} label="Task" tone="task">
              {trace.task}
            </Bubble>

            {trace.turns.map((turn) => (
              <Turn key={turn.turn} turn={turn} onZoom={setZoom} />
            ))}

            {trace.turns.length === 0 && (
              <p className="ccchat__empty">No request has been made yet.</p>
            )}

            <Bubble side="system" icon={<Terminal size={13} aria-hidden="true" />} label="Run">
              {trace.message || "(no message)"}
            </Bubble>
          </>
        )}
      </div>

      {zoom && (
        <div className="cczoom" role="dialog" aria-modal="true" aria-label={zoom.label} onClick={() => setZoom(null)}>
          <button type="button" className="cczoom__close" onClick={() => setZoom(null)}>
            <X size={16} aria-hidden="true" /> Close
          </button>
          <figure onClick={(e) => e.stopPropagation()}>
            <img src={zoom.src} alt={zoom.label} />
            <figcaption>{zoom.label}</figcaption>
          </figure>
        </div>
      )}
    </aside>
  );
}

function Turn({ turn, onZoom }: { turn: ComputerTurn; onZoom: (z: { src: string; label: string }) => void }) {
  return (
    <div className="ccchat__turn">
      <div className="ccchat__turn-rule">
        <span>Request #{turn.turn}</span>
        {turn.attempt > 0 && <span className="ccchat__retry">retry {turn.attempt + 1}</span>}
        {turn.first_turn && <span className="ccchat__tag">no screenshot yet</span>}
        <span className="ccchat__turn-time">{clock(turn.timestamp)}</span>
      </div>

      {/* 1 + 2.  What the model was handed. */}
      <Bubble side="user" icon={<User size={13} aria-hidden="true" />} label="AI input" tone="input">
        <div className="ccchat__facts">
          <Fact ok={turn.prompt_attached}>
            Computer-control prompt {turn.prompt_attached ? "attached" : "NOT attached"}
          </Fact>
          <Fact ok>
            {turn.message_count} {turn.message_count === 1 ? "message" : "messages"} of context
          </Fact>
          <Fact ok={turn.wire.image_present}>
            Screenshot on the wire: {turn.wire.image_present ? "yes" : "no"}
          </Fact>
          <Fact ok={turn.wire.messages_count === turn.message_count}>
            {turn.wire.messages_count} serialised for the API
          </Fact>
        </div>

        {turn.user_text && <Verbatim>{turn.user_text}</Verbatim>}

        {turn.screenshot_attached ? (
          <Shot
            image={turn.image}
            meta={turn.image_meta}
            caption="sent to the model"
            onZoom={onZoom}
          />
        ) : (
          <p className="ccchat__none">
            {turn.first_turn
              ? "No screenshot — this request happens before the first capture exists, so the model had nothing to look at yet."
              : "No screenshot was attached to this request."}
          </p>
        )}

        <Details summary="Full prompt and message list">
          {turn.prompt_attached ? (
            <Verbatim>{turn.prompt}</Verbatim>
          ) : (
            <p className="ccchat__none">No system message was sent with this request.</p>
          )}
          <ul className="ccchat__msgs">
            {turn.messages_meta.map((m, i) => (
              <li key={`${m.role}-${i}`}>
                <b>{m.role}</b> {m.chars} chars{m.images ? `, ${m.images} image` : ""}
                {m.content_preview && <Verbatim small>{m.content_preview}</Verbatim>}
              </li>
            ))}
          </ul>
        </Details>
      </Bubble>

      {/* 3.  The reply, exactly as it arrived. */}
      <Bubble side="ai" icon={<Bot size={13} aria-hidden="true" />} label="AI response" tone="raw">
        {turn.error ? (
          <p className="ccchat__none">The model was never reached: {turn.error}</p>
        ) : turn.raw === "" ? (
          <p className="ccchat__none">(the model returned an empty response)</p>
        ) : (
          <Verbatim raw>{turn.raw}</Verbatim>
        )}
      </Bubble>

      {/* 4.  What the parser made of it. */}
      {turn.raw !== "" && !turn.error && (
        <Bubble side="system" icon={<Cpu size={13} aria-hidden="true" />} label="Parsed command" tone={turn.parse_ok ? "ok" : "bad"}>
          {turn.parse_ok ? (
            <Verbatim small>{JSON.stringify(turn.command, null, 2)}</Verbatim>
          ) : (
            <p className="ccchat__none">Rejected, so nothing was executed. {turn.parse_error}</p>
          )}
        </Bubble>
      )}

      {/* 5.  What the machine did. */}
      {turn.execution && Object.keys(turn.execution).length > 0 && (
        <Bubble
          side="system"
          icon={iconFor(turn.execution.outcome)}
          label="Execution"
          tone={toneFor(turn.execution.outcome)}
        >
          <Execution turn={turn} />
        </Bubble>
      )}

      {/* 6.  The frame that followed. */}
      {turn.next_image && (
        <Bubble side="ai" icon={<Monitor size={13} aria-hidden="true" />} label="Next screenshot" tone="input">
          <Shot
            image={turn.next_image}
            meta={turn.next_image_meta}
            caption="captured after the action"
            onZoom={onZoom}
          />
          {turn.image_meta.sha256_16 === turn.next_image_meta.sha256_16 && (
            <p className="ccchat__warn">
              Identical to the frame the model was just given — nothing on screen changed.
            </p>
          )}
        </Bubble>
      )}
    </div>
  );
}

function Execution({ turn }: { turn: ComputerTurn }) {
  const ex = turn.execution;
  const cmd = ex.command as Record<string, unknown>;
  const type = String(cmd.type ?? "");
  const rows: [string, string][] = [];

  rows.push(["action", type || "(none)"]);

  if (type === "click" || type === "move") {
    rows.push(["asked for", `${fmt(cmd.x)}, ${fmt(cmd.y)}`]);
    rows.push([
      "pointer landed at",
      ex.actual_pointer_x == null ? "(not reported)" : `${ex.actual_pointer_x}, ${ex.actual_pointer_y}`,
    ]);
    if (ex.landed != null) rows.push(["landed as asked", ex.landed ? "yes" : "no"]);
    if (ex.screen_width) rows.push(["display", `${ex.screen_width} × ${ex.screen_height}`]);
  } else if (type === "type") {
    // The text is shown, and the fact that it was a type action is not hidden
    // by it: a run that typed the right characters into nothing looks exactly
    // like a run that worked.
    rows.push(["typed into", "whatever was focused in the remote browser"]);
    rows.push(["text", JSON.stringify(String(cmd.text ?? ""))]);
  } else if (type === "key") {
    rows.push(["key", String(cmd.key ?? "")]);
  } else if (type === "navigate") {
    rows.push(["url", String(cmd.url ?? "")]);
  } else if (type === "search") {
    rows.push(["query", String(cmd.query ?? "")]);
  } else if (type === "scroll") {
    rows.push(["delta_y", String(cmd.delta_y ?? "")]);
  } else if (type === "done" || type === "error") {
    rows.push(["message", String(cmd.message ?? "")]);
  }

  rows.push(["result", ex.outcome || "(unknown)"]);
  if (ex.result && ex.result !== ex.outcome) rows.push(["agent said", ex.result]);
  if (ex.duration_ms != null) rows.push(["took", `${ex.duration_ms} ms`]);
  if (ex.error) rows.push(["error", ex.error]);

  return (
    <>
      <div className="ccchat__facts">
        {rows.map(([k, v]) => (
          <Fact key={k} k={k} v={v} />
        ))}
      </div>
    </>
  );
}

function Shot({
  image,
  meta,
  caption,
  onZoom,
}: {
  image: string;
  meta: Partial<{ width: number; height: number; mime: string; bytes_b64: number; sha256_16: string }>;
  caption: string;
  onZoom: (z: { src: string; label: string }) => void;
}) {
  if (!image) {
    return (
      <p className="ccchat__warn">
        The frame exists in this turn but its bytes were withheld from this response
        {meta.sha256_16 ? ` (frame ${meta.sha256_16})` : ""}.
      </p>
    );
  }
  // The backend sniffs the type from the bytes, so this is a fallback for a
  // malformed record, not a normal path -- and it is left honest ("unknown")
  // rather than defaulted to jpeg, because a caption that guesses the format of
  // the image it is identifying is worse than one that admits it does not know.
  const mime = meta.mime || "unknown";
  const src = `data:${mime};base64,${image}`;
  const label = `Remote Chrome screenshot — ${meta.width ?? "?"} × ${meta.height ?? "?"} — ${mime} — frame ${meta.sha256_16 ?? "?"}`;
  return (
    <figure className="ccshot">
      <button type="button" className="ccshot__btn" onClick={() => onZoom({ src, label })} title="Open a larger preview">
        <img src={src} alt={label} />
      </button>
      <figcaption>
        <span className="ccshot__tag">Remote Chrome screenshot</span>
        <span className="ccshot__line">
          {meta.width ?? "?"} × {meta.height ?? "?"} &middot; {mime} &middot; {caption}
        </span>
        <span className="ccshot__line ccshot__line--hash">
          frame {meta.sha256_16 ?? "?"}
          {meta.bytes_b64 ? ` · ${formatBytes(meta.bytes_b64)}` : ""}
        </span>
      </figcaption>
    </figure>
  );
}

function Bubble({
  side,
  icon,
  label,
  tone,
  children,
}: {
  side: "user" | "ai" | "system";
  icon: React.ReactNode;
  label: string;
  tone?: "task" | "input" | "raw" | "ok" | "bad";
  children: React.ReactNode;
}) {
  return (
    <div className={`ccbubble ccbubble--${side}`} data-tone={tone ?? side}>
      <div className="ccbubble__who">
        <span className="ccbubble__avatar" aria-hidden="true">
          {icon}
        </span>
        <span className="ccbubble__label">{label}</span>
      </div>
      <div className="ccbubble__body">{children}</div>
    </div>
  );
}

/**
 * The verbatim block.  `raw` gets a heavier border because it is the one piece
 * of text in the panel that must never be touched.
 */
function Verbatim({
  children,
  raw,
  small,
}: {
  children: string;
  raw?: boolean;
  small?: boolean;
}) {
  return (
    <pre className="ccchat__raw" data-raw={raw ? "true" : undefined} data-small={small ? "true" : undefined}>
      {children}
    </pre>
  );
}

function Fact({ children, ok, k, v }: { children?: React.ReactNode; ok?: boolean; k?: string; v?: string }) {
  return (
    <span className="ccchat__fact" data-ok={ok === undefined ? undefined : String(ok)}>
      {k ? (
        <>
          <b>{k}</b> {v}
        </>
      ) : (
        children
      )}
    </span>
  );
}

function Details({ summary, children }: { summary: string; children: React.ReactNode }) {
  return (
    <details className="ccchat__details">
      <summary>
        <ChevronDown size={13} aria-hidden="true" /> {summary}
      </summary>
      <div className="ccchat__details-body">{children}</div>
    </details>
  );
}

function toneFor(outcome?: string): "ok" | "bad" {
  return outcome === "refused" || outcome === "stopped" ? "bad" : "ok";
}

function iconFor(outcome?: string) {
  if (outcome === "refused") return <X size={13} aria-hidden="true" />;
  if (outcome === "stopped") return <X size={13} aria-hidden="true" />;
  if (outcome === "done") return <Send size={13} aria-hidden="true" />;
  return <MousePointerClick size={13} aria-hidden="true" />;
}

function fmt(v: unknown): string {
  return v === null || v === undefined ? "?" : String(v);
}

function clock(seconds: number): string {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleTimeString();
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
