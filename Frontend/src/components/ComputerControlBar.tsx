import { useCallback, useEffect, useRef, useState } from "react";
import ComputerChat from "./ComputerChat";
import {
  fetchComputerProviders,
  fetchComputerTask,
  startComputerTask,
  stopComputerTask,
  type ComputerProvider,
  type ComputerRun,
  type ComputerStatus,
} from "../lib/screen";

/**
 * A small status line for the AI driving the real browser.
 *
 * Deliberately not a modal, a toast, or anything that takes focus.  The user is
 * watching the same Chrome window the model is clicking in; covering it up would
 * hide the very thing they want to see.  It also never blocks input, because
 * being unable to interrupt a machine that is clicking on your behalf is not a
 * feature.
 */
export default function ComputerControlIndicator() {
  const [run, setRun] = useState<ComputerRun | null>(null);
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [inspect, setInspect] = useState(false);
  const [providers, setProviders] = useState<ComputerProvider[]>([]);
  const [provider, setProvider] = useState("");
  const pollRef = useRef<number | null>(null);

  // The provider list is configuration, not run state: it does not change while
  // a task runs, and a failure here must not stop the status line from working.
  useEffect(() => {
    void fetchComputerProviders()
      .then((r) => {
        setProviders(r.providers);
        setProvider((current) => current || r.default || r.providers[0]?.name || "");
      })
      .catch(() => {
        /* the picker simply stays hidden; starting a run still works */
      });
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  // Polled rather than pushed: the run lives in the backend's memory, and a
  // status endpoint is a request the user can refresh out of with the same
  // button they already use for the rest of the desktop.
  useEffect(() => {
    if (!run?.running) {
      stopPolling();
      return;
    }
    pollRef.current = window.setInterval(async () => {
      try {
        setRun(await fetchComputerTask(run.task_id));
      } catch {
        /* transient: the next tick tries again */
      }
    }, 1500);
    return stopPolling;
  }, [run?.task_id, run?.running, stopPolling]);

  const start = async () => {
    const text = task.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    try {
      setRun(await startComputerTask(text, "", provider));
      setTask("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!run) return;
    try {
      await stopComputerTask(run.task_id);
    } catch {
      /* the run is already gone; nothing to stop */
    }
  };

  const status: ComputerStatus = run?.status ?? "idle";
  const show = status !== "idle";

  return (
    <div className="cc-bar" data-status={status}>
      {inspect && run && <ComputerChat taskId={run.task_id} />}
      {show && (
        <>
          <span className={`cc-dot cc-dot--${status}`} aria-hidden="true" />
          <span className="cc-label">{label(status)}</span>
          {run?.message && <span className="cc-message">{run.message}</span>}
          {run && run.steps > 0 && (
            <span className="cc-steps">
              {run.step} {run.step === 1 ? "step" : "steps"}
            </span>
          )}
          {run && (
            <button
              type="button"
              className="cc-inspect-toggle"
              onClick={() => setInspect((v) => !v)}
              aria-expanded={inspect}
            >
              {inspect ? "Hide AI trace" : "AI trace"}
            </button>
          )}
          {run?.running && (
            <button type="button" className="cc-stop" onClick={stop}>
              Stop
            </button>
          )}
        </>
      )}

      {/* Which provider answers the first request.  The same list, and the same
          "not configured" marking, as the picker inside the trace panel, so
          choosing here and switching there are not two different opinions about
          what is available. */}
      {!run?.running && providers.length > 0 && (
        <label className="cc-prov">
          <span className="cc-prov__label">Provider</span>
          <select
            className="cc-prov__select"
            value={provider}
            disabled={busy}
            onChange={(e) => setProvider(e.target.value)}
            title="Which provider answers the next computer-control request"
          >
            {providers.map((p) => (
              <option key={p.name} value={p.name}>
                {p.label}
                {p.configured ? "" : " — not configured"}
              </option>
            ))}
          </select>
        </label>
      )}

      <form
        className="cc-form"
        onSubmit={(e) => {
          e.preventDefault();
          void start();
        }}
      >
        <input
          className="cc-input"
          value={task}
          placeholder="Let the AI use the computer..."
          aria-label="Task for the AI to perform in the browser"
          onChange={(e) => setTask(e.target.value)}
          disabled={busy || run?.running}
        />
        <button type="submit" className="cc-go" disabled={busy || run?.running || !task.trim()}>
          {busy ? "Starting" : "Send"}
        </button>
      </form>

      {error && <span className="cc-error">{error}</span>}
    </div>
  );
}

function label(status: ComputerStatus): string {
  switch (status) {
    case "observing":
      return "AI observing";
    case "controlling":
      return "AI controlling computer";
    case "done":
      return "Task complete";
    case "error":
      return "AI stopped";
    default:
      return "";
  }
}
