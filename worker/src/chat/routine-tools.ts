/**
 * Worker-native routine tools for Chief chat.
 *
 * These are exposed to the model through Groq's native function-calling interface
 * (not parsed from text), so Chief can create and manage routines in real
 * conversation.  Every call is owner-scoped from the authenticated session --
 * the model never supplies an owner id -- and the D1 rows are the authority, so
 * a routine survives the Codespace being stopped and fires on the cron tick.
 */
import type { Env } from "../env";
import type { ToolCall, ToolDefinition } from "./types";
import {
  createRoutine,
  deleteRoutine,
  getRoutine,
  listRoutines,
  setRoutineEnabled,
  updateRoutine,
  type RoutineView,
} from "../routines";
import { describeSchedule } from "../recurrence";

function nowMs(): number {
  return Date.now();
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function intFrom(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : fallback;
}

/* ---------------------------------------------------------------- tool defs */

export const CREATE_ROUTINE_TOOL: ToolDefinition = {
  name: "create_routine",
  description:
    "Schedule a recurring task that runs automatically, even while you are offline. " +
    "Give a clear self-contained task prompt describing exactly what to do each time, " +
    "the schedule (daily or monthly), the time in HH:MM, and for monthly routines the " +
    "day of month (1-28). The recipient of any email the routine sends is always you " +
    "and cannot be changed.",
  parameters: {
    type: "object",
    properties: {
      name: {
        type: "string",
        description: "Short readable label for the routine (e.g. 'Daily research report').",
      },
      task_prompt: {
        type: "string",
        description:
          "Complete self-contained instructions for each run: objective, inputs, " +
          "workflow, verification, output format, constraints and failure handling.",
      },
      schedule_type: {
        type: "string",
        enum: ["daily", "monthly"],
        description: "Whether the routine runs every day or once a month.",
      },
      time: {
        type: "string",
        description: "Execution time in 24-hour HH:MM format, interpreted in the owner's timezone.",
      },
      day_of_month: {
        type: ["integer", "null"],
        minimum: 1,
        maximum: 28,
        description: "Day of month for monthly routines; null for daily routines.",
      },
      timezone: {
        type: "string",
        description: "IANA timezone like 'America/New_York'; defaults to the owner's timezone.",
      },
    },
    required: ["name", "task_prompt", "schedule_type", "time"],
    additionalProperties: false,
  },
};

export const LIST_ROUTINES_TOOL: ToolDefinition = {
  name: "list_routines",
  description: "List the routines you have scheduled, with their next run and status.",
  parameters: { type: "object", properties: {}, additionalProperties: false },
};

export const GET_ROUTINE_TOOL: ToolDefinition = {
  name: "get_routine",
  description: "Get one routine by id, including its full instructions and run history.",
  parameters: {
    type: "object",
    properties: {
      routine_id: { type: "string", description: "The routine id returned when it was created." },
    },
    required: ["routine_id"],
    additionalProperties: false,
  },
};

export const PAUSE_ROUTINE_TOOL: ToolDefinition = {
  name: "pause_routine",
  description: "Pause a routine so it starts no new runs until resumed.",
  parameters: {
    type: "object",
    properties: {
      routine_id: { type: "string", description: "The routine id to pause." },
    },
    required: ["routine_id"],
    additionalProperties: false,
  },
};

export const RESUME_ROUTINE_TOOL: ToolDefinition = {
  name: "resume_routine",
  description: "Resume a paused routine and recompute its next run.",
  parameters: {
    type: "object",
    properties: {
      routine_id: { type: "string", description: "The routine id to resume." },
    },
    required: ["routine_id"],
    additionalProperties: false,
  },
};

export const DELETE_ROUTINE_TOOL: ToolDefinition = {
  name: "delete_routine",
  description: "Delete a routine permanently, including its run history.",
  parameters: {
    type: "object",
    properties: {
      routine_id: { type: "string", description: "The routine id to delete." },
    },
    required: ["routine_id"],
    additionalProperties: false,
  },
};

export const ROUTINE_TOOLS: ToolDefinition[] = [
  CREATE_ROUTINE_TOOL,
  LIST_ROUTINES_TOOL,
  GET_ROUTINE_TOOL,
  PAUSE_ROUTINE_TOOL,
  RESUME_ROUTINE_TOOL,
  DELETE_ROUTINE_TOOL,
];

const TOOL_NAMES = new Set(ROUTINE_TOOLS.map((t) => t.name));

/* ---------------------------------------------------------------- execution */

export interface ToolResult {
  ok: boolean;
  output: string;
  error?: string;
  available?: boolean;
  routine?: RoutineView;
}

function routineText(r: RoutineView): string {
  const next = r.next_run_at
    ? new Date(r.next_run_at).toLocaleString(undefined, {
        dateStyle: "short",
        timeStyle: "short",
        timeZoneName: "short",
      })
    : "none";
  const active = r.enabled === 1 ? "active" : "paused";
  return `[${r.routine_id}] ${r.name} — ${r.schedule_text || r.schedule_type} · ${active} · next ${next} · last ${r.last_run_status || "never run"}`;
}

function scheduleFromArgs(args: Record<string, unknown>): { schedule_type: string; schedule_definition: unknown; ambiguous?: boolean; error?: string } {
  const schedule_type = str(args.schedule_type);
  const time = str(args.time);
  const day = args.day_of_month;

  let definition: Record<string, unknown>;
  if (schedule_type === "daily") {
    if (!/^([01]?[0-9]|2[0-3]):[0-5][0-9]$/.test(time)) {
      return { schedule_type, schedule_definition: {}, error: "daily routines need a time like '09:00'" };
    }
    definition = { type: "daily", time };
  } else if (schedule_type === "monthly") {
    if (!/^([01]?[0-9]|2[0-3]):[0-5][0-9]$/.test(time)) {
      return { schedule_type, schedule_definition: {}, error: "monthly routines need a time like '09:00'" };
    }
    const d = typeof day === "number" ? day : day !== null && day !== undefined ? Number(day) : NaN;
    if (!Number.isFinite(d) || d < 1 || d > 28) {
      return { schedule_type, schedule_definition: {}, error: "monthly routines need a day of month between 1 and 28" };
    }
    definition = { type: "monthly", time, day: Math.floor(d) };
  } else {
    return { schedule_type, schedule_definition: {}, error: "schedule_type must be 'daily' or 'monthly'" };
  }

  return { schedule_type, schedule_definition: definition };
}

export async function executeRoutineTool(
  env: Env,
  ownerSub: string,
  ownerId: string,
  agentId: string,
  ownerTimezone: string,
  call: ToolCall,
): Promise<ToolResult> {
  if (!TOOL_NAMES.has(call.name)) {
    return { ok: false, error: "The requested routine tool is not available." };
  }

  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(call.arguments || "{}");
  } catch {
    return { ok: false, error: "The routine tool arguments were malformed." };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: "The routine tool arguments were malformed." };
  }
  const args = parsed as Record<string, unknown>;

  if (call.name === "list_routines") {
    try {
      const rows = await listRoutines(env, ownerSub);
      if (!rows.length) return { ok: true, output: "No routines are scheduled yet." };
      return { ok: true, output: rows.map(routineText).join("\n") };
    } catch (error) {
      return { ok: false, error: `Could not list routines: ${(error as Error).message}` };
    }
  }

  const routineId = str(args.routine_id);
  if ((call.name === "get_routine" || call.name === "pause_routine" || call.name === "resume_routine" || call.name === "delete_routine") && !routineId) {
    return { ok: false, error: "routine_id is required." };
  }

  try {
    if (call.name === "create_routine") {
      const name = str(args.name).trim() || "Routine";
      const taskPrompt = str(args.task_prompt).trim();
      if (!taskPrompt) {
        return { ok: false, error: "task_prompt is required — describe what the routine should do." };
      }
      const schedule = scheduleFromArgs(args);
      if (schedule.error) {
        return { ok: false, error: schedule.error };
      }
      const timezone = str(args.timezone) || ownerTimezone || "UTC";
      const created = await createRoutine(env, {
        owner_sub: ownerSub,
        owner_id: ownerId,
        agent_id: agentId,
        name,
        task_prompt: taskPrompt,
        schedule_type: schedule.schedule_type,
        schedule_definition: schedule.schedule_definition,
        timezone,
      }, nowMs());
      if (!created.ok) {
        return { ok: false, error: created.error };
      }
      return {
        ok: true,
        output: `Created: ${routineText(created.routine)}`,
        routine: created.routine,
      };
    }

    if (call.name === "get_routine") {
      const row = await getRoutine(env, routineId);
      if (!row) return { ok: false, error: "No such routine." };
      if (row.owner_sub !== ownerSub) return { ok: false, error: "No such routine." };
      return {
        ok: true,
        output: `${routineText(row)}\n\nInstructions:\n${row.task_prompt}`,
        routine: row,
      };
    }

    if (call.name === "pause_routine" || call.name === "resume_routine") {
      const row = await getRoutine(env, routineId);
      if (!row) return { ok: false, error: "No such routine." };
      if (row.owner_sub !== ownerSub) return { ok: false, error: "No such routine." };
      const enabled = call.name === "resume_routine";
      const updated = await setRoutineEnabled(env, routineId, enabled, nowMs());
      if (!updated) return { ok: false, error: "Could not change the routine." };
      return { ok: true, output: `${enabled ? "Resumed" : "Paused"}: ${routineText(updated)}`, routine: updated };
    }

    if (call.name === "delete_routine") {
      const row = await getRoutine(env, routineId);
      if (!row) return { ok: false, error: "No such routine." };
      if (row.owner_sub !== ownerSub) return { ok: false, error: "No such routine." };
      const removed = await deleteRoutine(env, routineId, nowMs());
      if (!removed) return { ok: false, error: "Could not delete the routine." };
      return { ok: true, output: `Deleted routine ${routineId}.` };
    }

    return { ok: false, error: `Unknown routine tool ${call.name}.` };
  } catch (error) {
    return { ok: false, error: `The routine service failed: ${(error as Error).message}` };
  }
}

export function isRoutineTool(name: string): boolean {
  return TOOL_NAMES.has(name);
}
