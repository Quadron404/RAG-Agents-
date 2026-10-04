import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  Bot,
  ChevronDown,
  Copy,
  Cpu,
  Loader2,
  Monitor,
  MousePointerClick,
  Play,
  Send,
  Terminal,
  User,
  X,
} from "lucide-react";
import {
  fetchComputerTrace,
  setComputerProvider,
  type ComputerProvider,
  type ComputerTrace,
  type ComputerTurn,
} from "../lib/screen";
import { useComputerRun, useRunPolling } from "../lib/computerRun";

/**
 * The Computer AI panel: the computer-use loop as a conversation you can watch.
 *
 * The point is to answer "what is the AI actually seeing and saying, right
 * now" without opening a log file.  So this is a timeline, not a table: the
 * task, then for every request the input the model was handed, the bytes it
 * sent back, what the parser made of them, what the machine did, and the frame
 * that came after.
 *
 * It is a column of the Computer page's layout rather than a popup over it,
 * and it is mounted whether or not a task has ever been started.  Both of those
 * are load-bearing.  As a popup anchored above the status bar it sat behind the
 * dock, and it only existed once somebody pressed a button labelled "AI trace"
 * -- so the panel that exists to explain a run was itself the easiest thing in
 * the product to fail to find, at the moment of finding out a run had failed.
 * Mounted unconditionally, the no-run case says what it is instead of not
 * rendering.
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
export default function ComputerChat() {
  useRunPolling();
  const run = useComputerRun((s) => s.run);
  const task = useComputerRun((s) => s.task);
  const busy = useComputerRun((s) => s.busy);
  const error = useComputerRun((s) => s.error);
  const start = useComputerRun((s) => s.start);
  const stop = useComputerRun((s) => s.stop);
  const providers = useComputerRun((s) => s.providers);
  const provider = useComputerRun((s) => s.provider);
  const setProvider = useComputerRun((s) => s.setProvider);
  const setTask = useComputerRun((s) => s.setTask);

  const [trace, setTrace] = useState<ComputerTrace | null>(null);
  const [traceError, setTraceError] = useState("");
  const [zoom, setZoom] = useState<{ src: string; label: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const taskId = run?.task_id ?? "";

  const load = useCallback(async () => {
    // Nothing to read yet is not an error: it is the state before the first task.
    if (!taskId) {
      setTrace(null);
      setTraceError("");
      return;
    }
    try {
      setTrace(await fetchComputerTrace(taskId));
      setTraceError("");
    } catch (exc) {
      setTraceError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [taskId]);

  // Polled, so the panel fills in while the run is still going.  A trace read
  // once at the end misses the turn that went wrong, which is usually not the
  // last one.
  useEffect(() => {
    void load();
    if (!taskId) return;
    const timer = window.setInterval(() => void load(), 1000);
    return () => window.clearInterval(timer);
  }, [load, taskId]);

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

  /**
   * Re-pin after the screenshots decode.
   *
   * Pinning on the trace alone lands too early: the turn markup is in the DOM
   * before a single <img> has decoded, and every frame that arrives afterwards
   * pushes the newest turn further down. The result is a live log that appears
   * to follow the run and then quietly stops, parked above the last reply.
   */
  const repin = useCallback(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, []);

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

  /** Who answered, and with what, even before a run exists to say so. */
  const headerProvider = trace?.provider || provider;
  const headerModel = trace?.last_model || providers.find((p) => p.name === headerProvider)?.model || "";

  /* The run's failure, if it has one.  A message is required as well as the
     key: the backend omits `failure` on a clean run, but an empty object would
     still be truthy here and would raise a failure banner over a `done` run. */
  const failure = trace?.failure && trace.failure.message ? trace.failure : null;

  return (
    <aside className="ccchat" aria-label="Computer AI chat: what the AI receives and sends">
      <header className="ccchat__head">
        <div className="ccchat__title">
          <Bot size={15} aria-hidden="true" />
          <span>Computer AI</span>
          <span className="ccchat__count">
            {trace ? `${trace.turns.length} ${trace.turns.length === 1 ? "request" : "requests"}` : "—"}
          </span>
        </div>
        <div className="ccchat__head-actions">
          <ProviderPicker
            trace={trace}
            providers={providers}
            selected={provider}
            onSelect={setProvider}
            onChanged={() => void load()}
          />
          <button type="button" onClick={() => void copy()} disabled={!trace} title="Copy the whole trace as JSON">
            <Copy size={13} aria-hidden="true" />
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </header>

      {/* Provider and model named at the top of the panel, not only on each
          request.  Shown even with no run, so a misconfigured provider is
          visible before a task is spent discovering it. */}
      <div className="ccchat__whoami">
        <span>
          Provider <b>{labelFor(providers, headerProvider)}</b>
        </span>
        <span>
          Model <b className="ccpick__mono">{headerModel || "(not configured)"}</b>
        </span>
      </div>

      <div className="ccchat__scroll" ref={scroller} onScroll={onScroll}>
        {traceError && <p className="ccchat__error">Cannot read the trace: {traceError}</p>}
        {error && <p className="ccchat__error">{error}</p>}

        {!trace && !traceError && (
          <div className="ccchat__intro">
            <p className="ccchat__intro-lead">
              Every request the AI makes and every reply it gets back: the screenshot it was shown,
              the exact bytes it returned, what was parsed from them, and what the computer did.
            </p>
            <p>Start a task below. Each step appears here as it happens.</p>
          </div>
        )}

        {trace && (
          <>
            <div className="ccchat__meta">
              <span className={`ccchat__status ccchat__status--${trace.status}`}>{trace.status}</span>
              <span title="The provider and model that answered the most recent request below">
                {trace.last_provider || "—"} &middot; {trace.last_model || "—"}
              </span>
            </div>

            {trace.status === "stopped" && (
              <div className="ccchat__stopbox" role="status" aria-live="polite">
                <div className="ccchat__stopbox-title">
                  <span className="ccchat__stopbox-dot" aria-hidden="true" />
                  AI has stopped
                </div>
                <div className="ccchat__stopbox-text">
                  {trace.message || "No more API calls will be made for this task."}
                </div>
              </div>
            )}

            {/* The run's outcome, stated at the top as well as at the failing
                request below.  A run that died on its last turn is otherwise
                only readable by scrolling to the end of a long transcript, and
                the cause is the one thing anybody opens this panel to find.

                The `failure` key is optional and is only sent when the run
                really failed.  It is still checked for a message as well as
                for presence, because an empty object is truthy in JavaScript:
                rendering on presence alone put a confident "Run failed" banner
                above runs that finished with done. */}
            {failure && (
              <div className="ccfail ccfail--run" role="status">
                <p className="ccfail__headline">
                  {failure.provider_reached === true
                    ? `Provider reached — HTTP ${failure.http_status}${failure.http_reason ? ` ${failure.http_reason}` : ""}`
                    : failure.provider_reached === false
                      ? "Provider was not reached"
                      : "Run failed"}
                  {failure.final_result && <span className="ccfail__tag">{failure.final_result}</span>}
                </p>
                {failure.provider_error && <p className="ccfail__why">{failure.provider_error}</p>}
                <p className="ccfail__hint">
                  Request {failure.turn} of {trace.turns.length} &middot;{" "}
                  {failure.provider || "no provider"} &middot;{" "}
                  {(failure.retry_attempts ?? 1) > 1 ? `${failure.retry_attempts} attempts` : "1 attempt"}
                  . The failing request below has the full account.
                </p>
              </div>
            )}

            {/* The task, exactly as it was submitted.  It is also repeated in
                every request's history, but the conversation needs it stated
                once at the top to read as a conversation. */}
            <Bubble side="user" icon={<User size={13} aria-hidden="true" />} label="User task" tone="task">
              {trace.task}
            </Bubble>

            {trace.turns.map((turn) => (
              <Turn key={turn.turn} turn={turn} onZoom={setZoom} onGrown={repin} />
            ))}

            {trace.turns.length === 0 && (
              <p className="ccchat__empty">The run has been accepted but has not asked the model anything yet.</p>
            )}

            <Bubble side="system" icon={<Terminal size={13} aria-hidden="true" />} label="Run">
              {trace.message || "(no message)"}
            </Bubble>
          </>
        )}
      </div>

      {/* The composer lives with the transcript rather than in the status bar.
          The two belong together: you write the task here and read what it
          caused a few inches below, and a run started from anywhere else still
          shows its outcome in this panel. */}
      <form
        className="ccchat__compose"
        onSubmit={(e) => {
          e.preventDefault();
          void start(task);
        }}
      >
        {run?.running ? (
          <button type="button" className="ccchat__stop" onClick={() => void stop()}>
            <X size={13} aria-hidden="true" /> Stop the run
          </button>
        ) : (
          <>
            <input
              className="ccchat__task"
              value={task}
              placeholder="Tell the AI what to do on the computer…"
              aria-label="Task for the AI to perform in the browser"
              onChange={(e) => setTask(e.target.value)}
              disabled={busy || run?.running}
            />
            <button type="submit" className="ccchat__send" disabled={busy || run?.running || !task.trim()}>
              {busy ? (
                <Loader2 size={13} className="ccchat__spin" aria-hidden="true" />
              ) : (
                <Play size={13} aria-hidden="true" />
              )}
              {busy ? "Starting" : "Send"}
            </button>
          </>
        )}
      </form>

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

function labelFor(providers: ComputerProvider[], name: string): string {
  if (!name) return "—";
  return providers.find((p) => p.name === name)?.label ?? name;
}

/**
 * Which provider answers the next request.
 *
 * Shows both the provider and the model it will use, because "Mistral" alone
 * does not say which of several vision models just aimed at a screenshot, and
 * the model name is what differs between two runs that both claim to have
 * worked.
 *
 * Before a run exists this sets the provider the *next* task will use; once one
 * is running it switches that run, taking effect on the next request without
 * discarding the conversation.
 *
 * A provider with no key is offered but not hidden -- it is listed and marked,
 * and selecting it fails with the reason from the server.  Hiding it would
 * leave somebody staring at a selector with one option wondering what the other
 * one is, and would make a missing Codespaces secret invisible until they went
 * looking for it.
 */
function ProviderPicker({
  trace,
  providers,
  selected,
  onSelect,
  onChanged,
}: {
  trace: ComputerTrace | null;
  providers: ComputerProvider[];
  selected: string;
  onSelect: (name: string) => void;
  onChanged: () => void;
}) {
  const options = trace?.selected_providers?.length ? trace.selected_providers : providers;
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (options.length === 0) return null;

  const current = (trace ? trace.provider || options[0]?.name : selected || options[0]?.name) ?? "";
  const info = options.find((o) => o.name === current);
  // The model that answered last, so a switch is visible in the header before
  // the next request has even been made.
  const last = trace?.last_provider ? options.find((o) => o.name === trace.last_provider) : undefined;

  const choose = async (name: string) => {
    if (name === current) return;
    setBusy(true);
    setError("");
    try {
      if (trace) await setComputerProvider(trace.task_id, name);
      onSelect(name);
      onChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ccpick" title={error || `Next request goes to ${info?.label ?? current}`}>
      <label className="ccpick__label" htmlFor="cc-provider">
        Provider
      </label>
      <div className="ccpick__row">
        <select
          id="cc-provider"
          className="ccpick__select"
          value={current}
          disabled={busy}
          onChange={(e) => void choose(e.target.value)}
        >
          {options.map((o) => (
            <option key={o.name} value={o.name}>
              {o.label}
              {o.configured ? "" : " — not configured"}
            </option>
          ))}
        </select>
        {busy ? <Loader2 size={13} className="ccchat__spin" aria-hidden="true" /> : null}
      </div>
      {error && <div className="ccpick__bad">{error}</div>}
      {last && last.name !== current && (
        <div className="ccpick__was">
          last reply came from {last.label} &middot; <span className="ccpick__mono">{trace?.last_model}</span>
        </div>
      )}
    </div>
  );
}

/**
 * A request that was refused and then answered anyway.
 *
 * The reply is real and the run carried on, so nothing is broken -- but the
 * reader is looking at a success with no way to know the provider had refused
 * it first. That hides exactly the thing worth noticing: a provider that
 * refuses one request in three is about to refuse one in one, and the evidence
 * is right there in `http_attempts`.
 */
function Recovered({ attempts, turn }: { attempts: NonNullable<ComputerTurn["http_attempts"]>; turn: ComputerTurn }) {
  const last = attempts[attempts.length - 1];
  return (
    <div className="ccfail ccfail--recovered">
      <p className="ccfail__headline">
        Refused {attempts.length === 1 ? "once" : `${attempts.length} times`}, then answered
        <span className="ccfail__tag">retried</span>
      </p>
      <p className="ccfail__hint">
        {last.http_status ? (
          <>
            HTTP {last.http_status}
            {last.http_reason ? ` ${last.http_reason}` : ""}
            {last.provider_error ? ` — ${last.provider_error}` : ""}
            {last.retry_after ? `. The provider asked to wait ${Number(last.retry_after).toFixed(0)}s, and this reply arrived after that pause` : ""}.
          </>
        ) : (
          <>The provider refused, the request was re-sent, and this is the reply that came back.</>
        )}{" "}
        {turn.provider && `Answered by ${turn.provider}.`}
      </p>
    </div>
  );
}

/**
 * Why a request failed, told apart by cause instead of by prose.
 *
 * The wording used to be a fixed "the model was never reached" in front of
 * whatever the error said, and that sentence is false for the most common
 * failure there is: a 429 or a 5xx is the provider *having* been reached and
 * having refused. Sending the reader after the network when the cause was a
 * rate limit that clears on its own is the specific misdiagnosis this replaces.
 *
 * The fields come from the backend as separate values precisely so the panel
 * does not have to parse them back out of a sentence.
 */
function Failure({ turn }: { turn: ComputerTurn }) {
  const status = turn.http_status ?? 0;
  const reached = turn.provider_reached;
  const attempts = turn.http_attempts ?? [];
  const rateLimited = status === 429;

  const headline =
    reached === true
      ? `Provider reached — HTTP ${status}${turn.http_reason ? ` ${turn.http_reason}` : ""}`
      : reached === false
        ? "Provider was not reached"
        : turn.error;

  return (
    <div className="ccfail">
      <p className="ccfail__headline">
        {headline}
        {rateLimited && <span className="ccfail__tag">rate limited</span>}
      </p>

      {turn.provider_error && <p className="ccfail__why">{turn.provider_error}</p>}

      <div className="ccfail__facts">
        <Fact ok={Boolean(turn.provider)}>Provider: {turn.provider || "unknown"}</Fact>
        <Fact ok={Boolean(turn.model)}>
          Model: <span className="ccpick__mono">{turn.model || "unknown"}</span>
        </Fact>
        {status > 0 && (
          <Fact ok={false}>
            HTTP status: {status}
            {turn.http_reason ? ` ${turn.http_reason}` : ""}
          </Fact>
        )}
        {turn.retry_after !== undefined && turn.retry_after !== null && (
          <Fact ok>
            Provider asked to wait: {Number(turn.retry_after).toFixed(Number(turn.retry_after) % 1 ? 1 : 0)}s
          </Fact>
        )}
        <Fact ok={(turn.retry_attempts ?? 0) <= 1}>
          Attempts made: {turn.retry_attempts ?? 1}
        </Fact>
      </div>

      {rateLimited && (
        <p className="ccfail__hint">
          The provider was reached and refused on purpose, so this is not a network or a key problem.
          Quota limits clear on their own; the request is retried with a widening, jittered pause and
          then stops rather than retrying forever.
        </p>
      )}
      {status > 0 && !rateLimited && status >= 400 && status < 500 && (
        <p className="ccfail__hint">
          A 4xx means this request was refused and re-sending it unchanged would be refused the same
          way. Check the provider key and the model name before starting another run.
        </p>
      )}

      {attempts.length > 1 && (
        <Details summary={`Every refused attempt (${attempts.length})`}>
          <ol className="ccfail__attempts">
            {attempts.map((a) => (
              <li key={a.attempt}>
                <b>Attempt {a.attempt}</b> — HTTP {a.http_status}
                {a.http_reason ? ` ${a.http_reason}` : ""}
                {a.retryable ? " · retried" : " · not retried"}
                {a.provider_error ? ` · ${a.provider_error}` : ""}
              </li>
            ))}
          </ol>
        </Details>
      )}

      {turn.provider_error_raw && turn.provider_error_raw !== turn.provider_error && (
        <Details summary="Provider response body, verbatim">
          <Verbatim small>{turn.provider_error_raw}</Verbatim>
        </Details>
      )}

      {!turn.provider_error && !status && (
        <p className="ccchat__none">{turn.error}</p>
      )}
    </div>
  );
}

function Turn({ turn, onZoom, onGrown }: { turn: ComputerTurn; onZoom: (z: { src: string; label: string }) => void; onGrown: () => void }) {
  const serialised = serialisedFact(turn);
  return (
    <div className="ccchat__turn">
      <div className="ccchat__turn-rule">
        <span>Request #{turn.turn}</span>
        {/* Which provider answered this one.  On the turn, not just in the
            header: a run can switch provider mid-flight, and a header that
            shows only the current choice would misattribute every earlier
            reply to it. */}
        {turn.provider && <span className="ccchat__prov">{turn.provider}</span>}
        {turn.model && <span className="ccchat__provmodel">{turn.model}</span>}
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
          {/* The screenshot and the serialisation are separate facts.  "No
              screenshot on the wire" is a fact about this turn, not a fault:
              nothing attaches one unless the model asked to look, and the first
              turn always says no.  It is coloured from the value the serialiser
              reported and nothing else. */}
          <Fact ok={turn.wire?.image_present}>
            Screenshot on the wire: {turn.wire?.image_present ? "yes" : "no"}
          </Fact>
          <Fact ok={serialised.ok}>{serialised.text}</Fact>
        </div>

        {turn.user_text && <Verbatim>{turn.user_text}</Verbatim>}

        {turn.screenshot_attached ? (
          <Shot image={turn.image} meta={turn.image_meta} caption="sent to the model" onZoom={onZoom} onGrown={onGrown} />
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
        {/* Why the provider stopped, in its own word.  Neutral, because it is a
            reported value and not a verdict -- but it is the whole difference
            between an empty reply that was truncated by the completion ceiling
            and one that carried a call the parser could not read, so it is shown
            on every turn rather than only on the ones that failed. */}
        {!turn.error && (
          <div className="ccchat__facts">
            <Fact>finish_reason: {turn.stop_reason || "not reported by the provider"}</Fact>
          </div>
        )}
        {turn.error ? (
          <Failure turn={turn} />
        ) : turn.raw === "" ? (
          /* A native tool-calling reply has no assistant text at all -- these
             endpoints answer a tool call with `content: null` -- so an empty
             `raw` here means "the whole reply was the tool call", which is a
             complete answer, not an empty one. Calling it empty would report
             the normal case as the failure. */
          turn.tool_call?.name ? (
            <p className="ccchat__none">
              No assistant text. The reply was the native tool call{" "}
              <b>{turn.tool_call.name}</b>, whose arguments carry the run&apos;s history.
            </p>
          ) : (
            /* The runner's own diagnosis, which names the provider, the model
               and the reason it gave for stopping.  The old sentence here was
               "(the model returned an empty response)" for every one of these --
               a truncated reasoning trace, an unreadable call and a withheld
               answer all rendered as the same words, none of which said what to
               change. */
            <p className="ccchat__none">
              {turn.parse_error || "The reply carried no text and no tool call."}
            </p>
          )
        ) : (
          <>
            {/* A request that was refused and then answered on a retry has a
                perfectly good reply above and no sign at all that it was ever
                refused, which makes the retry policy invisible. The refusals
                are on the turn, so they are shown here rather than only when
                the retry budget finally ran out. */}
            {(turn.http_attempts?.length ?? 0) > 0 && <Recovered attempts={turn.http_attempts ?? []} turn={turn} />}
            <Verbatim raw>{turn.raw}</Verbatim>
          </>
        )}
      </Bubble>

      {/* 4.  What the parser made of it. */}
      {!turn.error && (turn.raw !== "" || !!turn.tool_call?.name) && (
        <Bubble
          side="system"
          icon={<Cpu size={13} aria-hidden="true" />}
          label="Parsed command"
          tone={turn.parse_ok ? "ok" : "bad"}
        >
          {turn.parse_ok ? (
            <Verbatim small>{JSON.stringify(turn.command, null, 2)}</Verbatim>
          ) : (
            <p className="ccchat__none">Rejected, so nothing was executed. {turn.parse_error}</p>
          )}
        </Bubble>
      )}

      {/* 5.  The sentence the model wrote about what it just issued.  This is
          History.txt: the exact entry the *next* request carries, so a run's
          memory is visible where the actions are.  Only the sentence, never the
          coordinates or the verdict -- those are the executor's, and they are in
          the Execution block below. */}
      {(turn.history_note || turn.history_error) && (
        <Bubble
          side="ai"
          icon={<Bot size={13} aria-hidden="true" />}
          label="AI history"
          tone={turn.history_note ? "ok" : "bad"}
        >
          {turn.history_note ? (
            <p className="ccchat__note">{turn.history_note}</p>
          ) : (
            <p className="ccchat__none">
              Not added to History.txt. {turn.history_error}
            </p>
          )}
        </Bubble>
      )}

      {/* 6.  What the machine did. */}
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

      {/* 7.  The frame that followed. */}
      {turn.next_image && (
        <Bubble side="ai" icon={<Monitor size={13} aria-hidden="true" />} label="Next screenshot" tone="input">
          <Shot image={turn.next_image} meta={turn.next_image_meta} caption="captured after the action" onZoom={onZoom} onGrown={onGrown} />
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
    <div className="ccchat__facts">
      {rows.map(([k, v]) => (
        <Fact key={k} k={k} v={v} />
      ))}
    </div>
  );
}

function Shot({
  image,
  meta,
  caption,
  onZoom,
  onGrown,
}: {
  image: string;
  meta: Partial<{ width: number; height: number; mime: string; bytes_b64: number; sha256_16: string }>;
  caption: string;
  onZoom: (z: { src: string; label: string }) => void;
  onGrown: () => void;
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
        <img src={src} alt={label} onLoad={onGrown} />
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

/**
 * The "serialised for the API" fact, decided by the backend's `serialized_ok`
 * and coloured to match.
 *
 * The panel used to decide this itself, by comparing the wire's message count
 * against the runner's.  That is a comparison the frontend can only lose: on a
 * turn where the request was never serialised there is no count to compare, so
 * `undefined === 2` is false and a turn that sent nothing was reported as a turn
 * that sent the wrong number of messages -- red, on both halves of the same
 * badge.  A provider that was never configured got the same verdict as a
 * serialiser that dropped messages, and neither was the thing that happened.
 *
 * Three states, and the third is the one that was missing:
 *
 * - `true` when the serialiser says the body was serialisable and carried every
 *   message the runner built.
 * - `false` when it says otherwise, with the reason it gave -- an unencodable
 *   object, or a message list that came out shorter than it went in.
 * - `undefined` when there is nothing to judge: no request was made, or the
 *   backend is older than this field.  Neutral text, no colour, no verdict.  An
 *   unanswered question is not a failed serialisation.
 */
function serialisedFact(turn: ComputerTurn): { ok?: boolean; text: string } {
  const wire = turn.wire;
  const sent = typeof wire?.messages_count === "number" ? wire.messages_count : null;
  const built = typeof wire?.source_message_count === "number"
    ? wire.source_message_count
    : turn.message_count;

  if (!wire || sent === null) {
    return { ok: undefined, text: "No serialised request was recorded for this turn" };
  }

  // Independently of the backend's verdict: a count that disagrees with the
  // runner is a mismatch whatever the serialiser believed, and saying so
  // locally keeps the badge honest if the two ever drift apart.
  const mismatch = typeof built === "number" && sent !== built;
  if (wire.serialized_ok === false || mismatch) {
    const why = wire.serialization_error
      || (mismatch
        ? `the request carried ${sent} of the ${built} messages the runner built`
        : "the backend reported that the request body could not be serialised");
    return { ok: false, text: `Serialisation failed: ${why}` };
  }

  return {
    ok: wire.serialized_ok,
    text: `${sent} serialised for the API`,
  };
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