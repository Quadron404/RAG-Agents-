import { useCallback, useEffect, useState } from "react";
import {
  fetchComputerTrace,
  type ComputerTrace,
  type ComputerTurn,
} from "../lib/screen";

/**
 * The AI Inspector: what the model was actually given, and what it actually
 * said back.
 *
 * Built because a computer-control failure has four possible homes -- the
 * model, the prompt, the parser, or the executor -- and they all look identical
 * from a status line.  "AI stopped" after three seconds cannot tell you whether
 * the screenshot reached the API, whether the prompt arrived, whether the reply
 * was prose instead of JSON, or whether a click landed at all.
 *
 * Three rules this component does not break:
 *
 *  1. The raw reply is rendered as-is, in a monospace block.  Never reformatted,
 *     never summarised, never replaced with "the model replied with...".  The
 *     moment the text is prettied, the evidence for a non-compliant model is
 *     gone, and a non-compliant model is indistinguishable from a working one.
 *  2. Screenshot metadata is always shown, and the image itself only when one
 *     exists for that turn.  A turn with no screenshot says so rather than
 *     showing an empty frame.
 *  3. Nothing here is ever treated as proof on its own.  The wire summary is
 *     the request as serialised; the execution block is what the machine
 *     reported back.  Both are shown side by side with the reply so a
 *     disagreement between them is visible.
 */
export default function ComputerInspector({ taskId }: { taskId: string }) {
  const [trace, setTrace] = useState<ComputerTrace | null>(null);
  const [error, setError] = useState("");
  const [openTurn, setOpenTurn] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setTrace(await fetchComputerTrace(taskId));
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [taskId]);

  // Re-read as the run progresses.  A trace fetched once at the end would miss
  // the turn that went wrong, which is usually not the last one.
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 2000);
    return () => window.clearInterval(timer);
  }, [load]);

  const copy = async () => {
    if (!trace) return;
    await navigator.clipboard.writeText(JSON.stringify(trace, null, 2));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  if (error && !trace) {
    return (
      <div className="cc-inspector">
        <p className="cc-inspector-error">{error}</p>
      </div>
    );
  }
  if (!trace) return null;

  return (
    <div className="cc-inspector">
      <div className="cc-inspector-head">
        <span className="cc-inspector-title">AI Inspector</span>
        <span className="cc-inspector-sub">
          {trace.turns.length} {trace.turns.length === 1 ? "request" : "requests"} to the
          model &middot; {trace.status}
        </span>
        <button type="button" className="cc-inspector-copy" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy trace"}
        </button>
      </div>

      <dl className="cc-protocol">
        <div>
          <dt>First turn may</dt>
          <dd>{trace.protocol.first_turn_allowed.join(" | ")}</dd>
        </div>
        <div>
          <dt>With a screenshot it may act on what is visible</dt>
          <dd>{trace.protocol.after_screenshot_visible_target.join(" | ")}</dd>
        </div>
        <div>
          <dt>JSON only</dt>
          <dd>{trace.protocol.json_only ? "yes, parsed strictly" : "no"}</dd>
        </div>
      </dl>

      {trace.turns.map((turn) => (
        <Turn key={turn.turn} turn={turn} open={openTurn === turn.turn} onToggle={setOpenTurn} />
      ))}
    </div>
  );
}

function Turn({
  turn,
  open,
  onToggle,
}: {
  turn: ComputerTurn;
  open: boolean;
  onToggle: (turn: number | null) => void;
}) {
  return (
    <section className="cc-turn" data-open={open}>
      <button
        type="button"
        className="cc-turn-head"
        onClick={() => onToggle(open ? null : turn.turn)}
        aria-expanded={open}
      >
        <span className="cc-turn-n">#{turn.turn}</span>
        <span className="cc-turn-model">{turn.model || "no model reached"}</span>
        <Badges turn={turn} />
        <span className="cc-turn-caret" aria-hidden="true">
          {open ? "-" : "+"}
        </span>
      </button>

      {open && (
        <div className="cc-turn-body">
          <Block title="Model call">
            <Rows
              rows={[
                ["provider", turn.provider || "(none)"],
                ["model", turn.model || "(none)"],
                ["requested at", stamp(turn.timestamp)],
                ["replied at", stamp(turn.reply_timestamp)],
                ["first turn", turn.first_turn ? "yes, no screenshot yet" : "no"],
                ["retry", turn.attempt === 0 ? "first attempt" : `attempt ${turn.attempt + 1}`],
                ["transport error", turn.error || "none"],
              ]}
            />
          </Block>

          <Block title="Request as serialised" note="The body that was sent, not app state.">
            <Rows
              rows={[
                ["messages", `${turn.wire.messages_count} (from ${turn.message_count} built)`],
                ["roles", turn.wire.roles.join(" > ") || "(none)"],
                ["text parts", String(turn.wire.text_parts)],
                ["image on the wire", turn.wire.image_present ? "yes" : "no"],
                ["image parts", String(turn.wire.image_count)],
                ["image type", turn.wire.image_payload_type || "(none)"],
                ["image mime", turn.wire.image_mime || "(none)"],
                ["stream", turn.wire.stream ? "yes" : "no"],
                ["response_format", String(turn.wire.response_format ?? "not set")],
              ]}
            />
            {turn.wire.messages_count !== turn.wire.source_message_count && (
              <p className="cc-warn">
                {turn.wire.source_message_count} messages were built but{" "}
                {turn.wire.messages_count} were sent.
              </p>
            )}
          </Block>

          <Block title="Screenshot sent to the model">
            {turn.screenshot_attached ? (
              <>
                <Rows
                  rows={[
                    ["size", `${turn.image_meta.width} x ${turn.image_meta.height}`],
                    ["mime", turn.image_meta.mime || "(unknown)"],
                    ["base64 bytes", String(turn.image_meta.bytes_b64 ?? 0)],
                    ["frame id", turn.image_meta.sha256_16 || "(none)"],
                  ]}
                />
                {turn.image ? (
                  <img className="cc-shot" src={`data:image/${turn.image_meta.mime?.replace("image/", "") || "jpeg"};base64,${turn.image}`} alt={`Screenshot sent on request ${turn.turn}`} />
                ) : (
                  <p className="cc-warn">The bytes of this screenshot were withheld from this response.</p>
                )}
              </>
            ) : (
              <p className="cc-note">
                None. This turn happened before any capture existed, so the model had nothing to
                look at.{" "}
                {turn.first_turn
                  ? "It may only navigate or search."
                  : "This is a fault: a turn after a screenshot must carry one."}
              </p>
            )}
          </Block>

          <Block
            title="Prompt"
            note={
              turn.prompt_attached
                ? `Sent as the system message, in full.`
                : "Not attached."
            }
          >
            {turn.prompt_attached ? (
              <>
                <pre className="cc-prompt">{turn.prompt}</pre>
                <Rows
                  rows={[
                    ["prompt attached", "yes"],
                    ["screenshot attached", turn.screenshot_attached ? "yes" : "no"],
                    ["json only", turn.json_only ? "yes" : "no"],
                    ["allowed here", turn.allowed_types.join(" | ")],
                  ]}
                />
              </>
            ) : (
              <p className="cc-warn">No system message in this request. The model was not told the protocol.</p>
            )}
          </Block>

          <Block title="Text prompt, task, history and state">
            {turn.messages_meta.map((m, i) => (
              <div className="cc-msg" key={`${m.role}-${i}`}>
                <span className="cc-msg-role">{m.role}</span>
                <span className="cc-msg-size">
                  {m.chars} chars{m.images ? `, ${m.images} image` : ""}
                </span>
                <pre className="cc-msg-body">{m.content_preview || "(empty)"}</pre>
              </div>
            ))}
          </Block>

          <Block title="Raw model response" note="Exactly as it arrived. Not reformatted.">
            <pre className="cc-raw">{turn.raw === "" ? "(empty response)" : turn.raw}</pre>
            {turn.parse_ok ? (
              <p className="cc-ok">Parsed as valid JSON for this turn.</p>
            ) : (
              <p className="cc-warn">
                Rejected by the parser: {turn.parse_error || "no reason recorded"}
              </p>
            )}
          </Block>

          <Block title="Parsed command">
            <pre className="cc-cmd">
              {Object.keys(turn.command).length
                ? JSON.stringify(turn.command, null, 2)
                : "(nothing was executed)"}
            </pre>
          </Block>

          <Block title="What the executor did">
            {turn.execution && Object.keys(turn.execution).length ? (
              <Rows
                rows={[
                  ["outcome", turn.execution.outcome || "(unknown)"],
                  ["executed", turn.execution.executed ? "yes" : "no"],
                  ["asked for", coord(turn.execution.x, turn.execution.y)],
                  ["pointer landed at", coord(turn.execution.actual_pointer_x, turn.execution.actual_pointer_y)],
                  ["landed as asked", String(turn.execution.landed ?? "unknown")],
                  ["display", `${turn.execution.screen_width ?? "?"} x ${turn.execution.screen_height ?? "?"}`],
                  ["took", `${turn.execution.duration_ms ?? 0} ms`],
                  ["ended the run", turn.execution.terminal ? "yes" : "no"],
                  ["error", turn.execution.error || "none"],
                ]}
              />
            ) : (
              <p className="cc-note">No command reached the executor on this request.</p>
            )}
          </Block>

          <Block title="Screenshot after this action">
            {turn.next_image ? (
              <>
                <Rows
                  rows={[
                    ["size", `${turn.next_image_meta.width} x ${turn.next_image_meta.height}`],
                    ["frame id", turn.next_image_meta.sha256_16 || "(none)"],
                  ]}
                />
                <img
                  className="cc-shot"
                  src={`data:image/${turn.next_image_meta.mime?.replace("image/", "") || "jpeg"};base64,${turn.next_image}`}
                  alt={`Screenshot after request ${turn.turn}`}
                />
                {turn.image_meta.sha256_16 === turn.next_image_meta.sha256_16 && (
                  <p className="cc-warn">Identical to the screenshot this turn was given. The screen did not visibly change.</p>
                )}
              </>
            ) : (
              <p className="cc-note">
                None. Either the run ended on this turn, or no capture followed the action.
              </p>
            )}
          </Block>
        </div>
      )}
    </section>
  );
}

function Badges({ turn }: { turn: ComputerTurn }) {
  const outcome = turn.execution?.outcome;
  return (
    <span className="cc-badges">
      {turn.first_turn && <span className="cc-badge cc-badge--first">no screenshot</span>}
      {turn.attempt > 0 && <span className="cc-badge cc-badge--retry">retry {turn.attempt}</span>}
      {turn.error && <span className="cc-badge cc-badge--bad">transport error</span>}
      {turn.raw !== "" && !turn.parse_ok && (
        <span className="cc-badge cc-badge--bad">not JSON</span>
      )}
      {turn.parse_ok && <span className="cc-badge cc-badge--ok">valid JSON</span>}
      {outcome === "executed" && <span className="cc-badge cc-badge--ok">executed</span>}
      {outcome === "done" && <span className="cc-badge cc-badge--ok">done</span>}
      {outcome === "refused" && <span className="cc-badge cc-badge--bad">refused</span>}
      {outcome === "stopped" && <span className="cc-badge cc-badge--bad">stopped</span>}
    </span>
  );
}


function Block({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="cc-block">
      <h4 className="cc-block-title">
        {title}
        {note && <span className="cc-block-note">{note}</span>}
      </h4>
      {children}
    </div>
  );
}

function Rows({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="cc-rows">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function coord(x?: number | null, y?: number | null): string {
  if (x === null || x === undefined || y === null || y === undefined) return "(not a pointer action)";
  return `${x}, ${y}`;
}

function stamp(seconds: number): string {
  if (!seconds) return "(never)";
  return new Date(seconds * 1000).toLocaleTimeString();
}
