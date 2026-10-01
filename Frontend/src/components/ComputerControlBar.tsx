import { useComputerRun } from "../lib/computerRun";
import type { ComputerProvider, ComputerStatus } from "../lib/screen";

/**
 * The status line for the AI driving the real browser.
 *
 * Deliberately not a modal, a toast, or anything that takes focus, and now
 * deliberately not the place the AI transcript lives.  The user is watching
 * the same Chrome window the model is clicking in; covering it would hide the
 * thing they want to see.  It never blocks input either, because being unable
 * to interrupt a machine that is clicking on your behalf is not a feature.
 *
 * What it *is* is progress and controls for the run, at a glance.  The running
 * conversation with the model is the Computer AI panel beside the screen, which
 * is mounted whether or not this bar is.  When this bar used to host that
 * conversation as a popup, the panel's visibility depended on this component's
 * local state, and a trace that explained a failure could itself be the thing
 * that was missing.
 */
export default function ComputerControlBar() {
  const run = useComputerRun((s) => s.run);
  const busy = useComputerRun((s) => s.busy);
  const error = useComputerRun((s) => s.error);
  const stop = useComputerRun((s) => s.stop);
  const providers = useComputerRun((s) => s.providers);
  const provider = useComputerRun((s) => s.provider);
  const setProvider = useComputerRun((s) => s.setProvider);

  const status: ComputerStatus = run?.status ?? "idle";
  const show = status !== "idle";

  return (
    <div className="cc-bar" data-status={status}>
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
          {run?.running && (
            <button type="button" className="cc-stop" onClick={() => void stop()}>
              Stop
            </button>
          )}
        </>
      )}

      {/* Which provider answers the next request.  Also in the AI panel header;
          both read the same store, so they cannot disagree about what is
          available or what is selected. */}
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
            {providers.map((p: ComputerProvider) => (
              <option key={p.name} value={p.name}>
                {p.label}
                {p.configured ? "" : " — not configured"}
              </option>
            ))}
          </select>
        </label>
      )}

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