/**
 * Internal API the backend uses to manage routines.
 *
 * The backend is the only caller and it presents the shared
 * ``x-rag-internal-token``; the same secret the backend itself trusts from the
 * Worker, so the trust is symmetric and no new credential exists.  Every route
 * is behind that token -- a browser reaching this Worker on its own is rejected
 * before any routine is read or written.
 *
 * Note the direction: the authority for routines is here, at the edge, because
 * the schedule has to outlive the Codespace.  The backend tools are a thin,
 * authenticated client of this API, not a second copy of it.
 */
import type { Env } from "./env";
import { timingSafeEqual } from "./encoding";
import { json } from "./http";
import {
  createRoutine,
  deleteRoutine,
  getRoutine,
  listRoutines,
  listRuns,
  setRoutineEnabled,
  updateRoutine,
} from "./routines";
import { runScheduler } from "./scheduler";

function internalAuthorized(request: Request, env: Env): boolean {
  const supplied = request.headers.get("x-rag-internal-token") ?? "";
  const expected = env.RAG_INTERNAL_TOKEN ?? "";
  if (!expected) return false;
  return timingSafeEqual(supplied, expected);
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const value = await request.json();
    return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/**
 * Handle any ``/internal/...`` request.  Returns null when the path is not an
 * internal route, so the caller can fall through to the proxy/assets.
 */
export async function handleInternal(request: Request, env: Env, url: URL): Promise<Response | null> {
  const path = url.pathname;
  if (!path.startsWith("/internal/")) return null;

  if (!internalAuthorized(request, env)) {
    return json({ ok: false, error: "internal authentication required" }, 401);
  }

  const now = Date.now();

  if (path === "/internal/scheduler/run" && request.method === "POST") {
    const result = await runScheduler(env, now);
    return json({ ok: true, ...result });
  }

  if (path === "/internal/routines" && request.method === "POST") {
    const body = await readJson(request);
    const ownerSub = str(body.owner_sub).trim();
    if (!ownerSub) return json({ ok: false, error: "owner_sub is required", ambiguous: false }, 400);
    const created = await createRoutine(
      env,
      {
        owner_sub: ownerSub,
        owner_id: str(body.owner_id),
        agent_id: str(body.agent_id),
        name: str(body.name) || "Routine",
        task_prompt: str(body.task_prompt),
        schedule_type: str(body.schedule_type),
        schedule_definition: body.schedule ?? body.schedule_definition ?? {},
        timezone: str(body.timezone) || "UTC",
      },
      now,
    );
    if (!created.ok) {
      return json({ ok: false, error: created.error, ambiguous: created.ambiguous }, 400);
    }
    return json({ ok: true, routine: created.routine });
  }

  if (path === "/internal/routines" && request.method === "GET") {
    const ownerSub = (url.searchParams.get("owner_sub") ?? "").trim();
    if (!ownerSub) return json({ ok: false, error: "owner_sub is required" }, 400);
    return json({ ok: true, routines: await listRoutines(env, ownerSub) });
  }

  const match = /^\/internal\/routines\/([^/]+)(?:\/(runs|pause|resume))?$/.exec(path);
  if (match) {
    const routineId = decodeURIComponent(match[1]);
    const action = match[2] ?? "";
    const routine = await getRoutine(env, routineId);
    if (!routine) return json({ ok: false, error: "no such routine" }, 404);
    const ownerSub = (url.searchParams.get("owner_sub") ?? str((await readJsonSafe(request)).owner_sub)).trim();
    if (ownerSub && ownerSub !== routine.owner_sub) {
      return json({ ok: false, error: "no such routine" }, 404);
    }

    if (action === "runs" && request.method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? "20") || 20;
      return json({ ok: true, runs: await listRuns(env, routineId, limit) });
    }

    if (action === "pause" && request.method === "POST") {
      return json({ ok: true, routine: await setRoutineEnabled(env, routineId, false, now) });
    }

    if (action === "resume" && request.method === "POST") {
      const resumed = await setRoutineEnabled(env, routineId, true, now);
      if (resumed && resumed.schedule) {
        // setRoutineEnabled recomputes next_run_at; a schedule that can no
        // longer produce a future run cannot be resumed.
        if (resumed.next_run_at === null && resumed.schedule.type !== "once") {
          return json({ ok: false, error: "that schedule has no future occurrence" }, 400);
        }
      }
      return json({ ok: true, routine: resumed });
    }

    if (request.method === "GET" && !action) {
      return json({ ok: true, routine });
    }

    if (request.method === "PATCH" && !action) {
      const body = await readJson(request);
      const patch: Record<string, unknown> = {};
      if (body.name !== undefined) patch.name = str(body.name);
      if (body.task_prompt !== undefined) patch.task_prompt = str(body.task_prompt);
      if (body.schedule_type !== undefined) patch.schedule_type = str(body.schedule_type);
      if (body.schedule !== undefined) patch.schedule_definition = body.schedule;
      if (body.schedule_definition !== undefined) patch.schedule_definition = body.schedule_definition;
      if (body.timezone !== undefined) patch.timezone = str(body.timezone);
      const updated = await updateRoutine(env, routineId, patch, now);
      if (!updated.ok) return json({ ok: false, error: updated.error, ambiguous: updated.ambiguous }, 400);
      return json({ ok: true, routine: updated.routine });
    }

    if (request.method === "DELETE" && !action) {
      await deleteRoutine(env, routineId, now);
      return json({ ok: true });
    }
  }

  return json({ ok: false, error: "unknown internal route" }, 404);
}

async function readJsonSafe(request: Request): Promise<Record<string, unknown>> {
  // Only used to allow owner_sub to travel in a body on DELETE-less verbs; a
  // body that is absent or already consumed must not throw.
  try {
    return await readJson(request.clone());
  } catch {
    return {};
  }
}
