import { useCallback, useEffect, useState } from "react";
import {
  CalendarClock,
  ChevronDown,
  ChevronRight,
  Clock,
  Pause,
  Play,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useUi } from "../store";
import { relativeTime, timeStr } from "../lib/format";
import {
  deleteRoutine,
  fetchRoutineRuns,
  fetchRoutines,
  pauseRoutine,
  resumeRoutine,
  type Routine,
  type RoutineRun,
} from "../lib/routines";
import { IconButton, Pill, Spinner } from "./ui";

/* ============================================================================
   Scheduled routines.

   Routines are created and edited through chat; this panel is the read-and-
   manage surface for them.  It deliberately distinguishes three states that
   are easy to blur together:

     * loading      -- we do not know yet;
     * unavailable  -- the deployment has no edge routines service, which is a
                       configuration fact, not "you have none";
     * ready/empty  -- the service answered and there genuinely are none,
                       which is when an example is worth offering.

   Nothing here invents data: every row is a routine the edge returned, and the
   run history is fetched on demand rather than guessed.
   ========================================================================== */

type PanelState =
  | { kind: "loading" }
  | { kind: "unavailable"; error: string }
  | { kind: "error"; error: string }
  | { kind: "ready"; routines: Routine[] };

/** "in 4h", "in 2d", or a date once it is far enough out to read better. */
function untilTime(ts: number | null): string {
  if (!ts) return "";
  const diff = ts - Date.now() / 1000;
  if (diff <= 0) return "due now";
  if (diff < 3600) return `in ${Math.max(1, Math.round(diff / 60))}m`;
  if (diff < 86400) return `in ${Math.round(diff / 3600)}h`;
  if (diff < 604800) return `in ${Math.round(diff / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
}

function runTone(status: string): "green" | "amber" | "red" | "default" {
  switch (status) {
    case "completed":
      return "green";
    case "running":
    case "queued":
    case "pending":
    case "delayed":
      return "amber";
    case "failed":
    case "rejected":
      return "red";
    default:
      return "default";
  }
}

export function RoutinesPanel() {
  const [state, setState] = useState<PanelState>({ kind: "loading" });

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    const result = await fetchRoutines();
    if (!result.available) {
      setState({ kind: "unavailable", error: result.error });
    } else if (!result.ok) {
      setState({ kind: "error", error: result.error });
    } else {
      setState({ kind: "ready", routines: result.routines });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (state.kind === "loading") {
    return (
      <div className="routines__state">
        <Spinner size={22} />
        <span>Loading your schedule…</span>
      </div>
    );
  }

  if (state.kind === "unavailable") {
    return (
      <div className="routines__state routines__state--muted">
        <CalendarClock size={20} />
        <div>
          <div className="routines__state-title">Scheduling is not available here</div>
          <div className="routines__state-sub">
            {state.error || "This deployment has no routines service configured."} You can still ask for a
            task inline in Chat.
          </div>
        </div>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className="routines__state routines__state--muted">
        <CalendarClock size={20} />
        <div>
          <div className="routines__state-title">Could not load your schedule</div>
          <div className="routines__state-sub">{state.error}</div>
        </div>
        <button className="routines__retry" onClick={load}>
          <RefreshCw size={14} /> Try again
        </button>
      </div>
    );
  }

  if (!state.routines.length) {
    return (
      <div className="routines__state routines__state--empty">
        <CalendarClock size={22} />
        <div>
          <div className="routines__state-title">Nothing scheduled yet</div>
          <div className="routines__state-sub">
            Ask in Chat to set something recurring — for example,
            <span className="routines__example">
              “every weekday at 8am, check my inbox and post a summary.”
            </span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="routines">
      <div className="routines__bar">
        <span className="routines__count">
          {state.routines.filter((r) => r.enabled).length} active · {state.routines.length} total
        </span>
        <IconButton icon={RefreshCw} onClick={load} title="Refresh" ariaLabel="Refresh routines" />
      </div>
      <div className="routines__list">
        {state.routines.map((routine) => (
          <RoutineRow key={routine.routine_id} routine={routine} onChanged={load} />
        ))}
      </div>
    </div>
  );
}

function RoutineRow({ routine, onChanged }: { routine: Routine; onChanged: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [runs, setRuns] = useState<RoutineRun[] | null>(null);
  const [runsError, setRunsError] = useState("");
  const [busy, setBusy] = useState(false);

  const toast = useUi((s) => s.toast);
  const openSheet = useUi((s) => s.openSheet);

  const active = routine.enabled === 1;

  const toggleRuns = useCallback(async () => {
    const next = !expanded;
    setExpanded(next);
    if (next && runs === null) {
      const result = await fetchRoutineRuns(routine.routine_id);
      if (!result.available) setRunsError(result.error || "Run history is not available.");
      else if (!result.ok) setRunsError(result.error);
      else setRuns([]);
      setRuns(result.runs);
    }
  }, [expanded, runs, routine.routine_id]);

  const togglePause = useCallback(async () => {
    setBusy(true);
    const result = active ? await pauseRoutine(routine.routine_id) : await resumeRoutine(routine.routine_id);
    setBusy(false);
    if (!result.ok) {
      toast(result.error || "Could not change the routine.", "err");
      return;
    }
    toast(active ? "Routine paused." : "Routine resumed.", "ok");
    onChanged();
  }, [active, onChanged, routine.routine_id, toast]);

  const confirmDelete = useCallback(() => {
    openSheet("Delete this routine?", [
      {
        label: "Delete routine",
        danger: true,
        onSelect: async () => {
          const result = await deleteRoutine(routine.routine_id);
          if (!result.ok) toast(result.error || "Could not delete the routine.", "err");
          else {
            toast("Routine deleted.", "ok");
            onChanged();
          }
        },
      },
    ]);
  }, [onChanged, openSheet, routine.routine_id, toast]);

  return (
    <div className={`routine${active ? "" : " routine--paused"}`}>
      <div className="routine__head">
        <button
          className="routine__disclosure"
          onClick={toggleRuns}
          aria-expanded={expanded}
          aria-label={expanded ? "Hide run history" : "Show run history"}
        >
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>
        <div className="routine__ident">
          <div className="routine__name">
            {routine.name}
            <Pill tone={active ? "green" : "default"}>{active ? "Active" : "Paused"}</Pill>
          </div>
          <div className="routine__meta">
            <Clock size={12} />
            {routine.schedule_text || routine.schedule_type}
            <span className="routine__sep">·</span>
            {active && routine.next_run_at
              ? `next ${untilTime(routine.next_run_at)}`
              : active
                ? "no future run"
                : "paused"}
            {routine.last_run_at ? (
              <>
                <span className="routine__sep">·</span>
                last {relativeTime(routine.last_run_at)}
                {routine.last_run_status ? ` (${routine.last_run_status})` : ""}
              </>
            ) : null}
          </div>
        </div>
        <div className="routine__actions">
          <IconButton
            icon={active ? Pause : Play}
            onClick={togglePause}
            disabled={busy}
            title={active ? "Pause" : "Resume"}
            ariaLabel={active ? "Pause routine" : "Resume routine"}
          />
          <IconButton icon={Trash2} onClick={confirmDelete} title="Delete" ariaLabel="Delete routine" />
        </div>
      </div>

      {routine.task_prompt ? <div className="routine__prompt">{routine.task_prompt}</div> : null}

      {expanded ? (
        <div className="runs">
          {runs === null && !runsError ? (
            <div className="runs__msg">
              <Spinner size={14} /> Loading run history…
            </div>
          ) : runsError ? (
            <div className="runs__msg runs__msg--err">{runsError}</div>
          ) : runs && runs.length ? (
            runs.map((run) => (
              <div className="run-row" key={run.routine_run_id}>
                <Pill tone={runTone(run.status)}>{run.status}</Pill>
                <span className="run-row__when">
                  {timeStr(run.scheduled_for)}
                  <span className="routine__sep">·</span>
                  {relativeTime(run.scheduled_for)}
                </span>
                <span className="run-row__detail" title={run.result_summary || run.error_code}>
                  {run.error_code || run.result_summary || "—"}
                </span>
              </div>
            ))
          ) : (
            <div className="runs__msg">No runs recorded yet.</div>
          )}
        </div>
      ) : null}
    </div>
  );
}
