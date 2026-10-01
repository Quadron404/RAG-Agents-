import { useEffect } from "react";
import { create } from "zustand";
import {
  fetchComputerProviders,
  fetchComputerTask,
  startComputerTask,
  stopComputerTask,
  type ComputerProvider,
  type ComputerRun,
} from "./screen";

/**
 * The one place the computer-control run lives.
 *
 * This existed as local state inside ComputerControlBar, which is why the AI
 * transcript could only ever be mounted as that bar's own child: the task id
 * that /ai/computer/{task_id}/trace needs was not reachable from anywhere else.
 * The panel therefore had to be a popup anchored to a 720px status pill, and a
 * popup is invisible in exactly the situation it matters -- while the run is
 * going, the bar is the busiest thing on screen.
 *
 * Holding it here instead means the transcript is mounted by the Computer page
 * as a column of the layout, and the status bar is just another reader. Neither
 * one is load-bearing for the other, so neither can hide the other.
 */

interface ComputerRunState {
  run: ComputerRun | null;
  task: string;
  busy: boolean;
  error: string;
  providers: ComputerProvider[];
  provider: string;

  setTask: (text: string) => void;
  bootstrap: () => void;
  start: (text: string) => Promise<void>;
  stop: () => Promise<void>;
  setProvider: (name: string) => void;
  pollOnce: () => Promise<void>;
}

export const useComputerRun = create<ComputerRunState>((set, get) => ({
  run: null,
  task: "",
  busy: false,
  error: "",
  providers: [],
  provider: "",

  /**
   * The provider list is configuration, not run state: it does not change while
   * a task runs, and a failure here must not stop the transcript from rendering.
   */
  bootstrap: () => {
    if (get().providers.length > 0) return;
    void fetchComputerProviders()
      .then((r) => {
        set((s) => ({
          providers: r.providers,
          provider: s.provider || r.default || r.providers[0]?.name || "",
        }));
      })
      .catch(() => {
        /* the picker stays as it is; starting a run still reports the reason */
      });
  },

  start: async (text) => {
    const trimmed = text.trim();
    if (!trimmed || get().busy || get().run?.running) return;
    set({ busy: true, error: "" });
    try {
      const run = await startComputerTask(trimmed, "", get().provider);
      set({ run, task: "" });
    } catch (exc) {
      set({ error: exc instanceof Error ? exc.message : String(exc) });
    } finally {
      set({ busy: false });
    }
  },

  stop: async () => {
    const run = get().run;
    if (!run) return;
    try {
      await stopComputerTask(run.task_id);
    } catch {
      /* the run is already gone; nothing to stop */
    }
  },

  setTask: (text) => set({ task: text }),

  setProvider: (name) => set({ provider: name }),

  pollOnce: async () => {
    const run = get().run;
    if (!run) return;
    try {
      const next = await fetchComputerTask(run.task_id);
      // A run that ended stays in the store.  The transcript is the record of
      // what happened, and a panel that empties the moment the task finishes
      // destroys the evidence at exactly the point somebody wants to read it.
      if (next.status === "error" && next.message) set({ error: next.message });
      set({ run: next });
    } catch {
      /* transient: the next tick tries again */
    }
  },
}));

/**
 * Poll the run while it is going.  Called once from the transcript panel, which
 * is mounted for the whole time the Computer page is open -- so the status line
 * and the trace advance from the same tick instead of from two loops that can
 * disagree about the current step.
 */
export function useRunPolling(): void {
  const bootstrap = useComputerRun((s) => s.bootstrap);
  const pollOnce = useComputerRun((s) => s.pollOnce);
  const running = useComputerRun((s) => s.run?.running);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void pollOnce(), 1000);
    return () => window.clearInterval(timer);
  }, [running, pollOnce]);
}