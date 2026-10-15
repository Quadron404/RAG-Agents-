"""The routine tools an ordinary agent can call, and their execution.

Nine tools, in two groups:

* ``get_current_time`` -- the server's authoritative clock.  A model asked "how
  long until Friday?" guesses; this answers.  It is available even with no
  routine context, because knowing the time is useful on its own.
* the eight management tools, which speak to the Worker's internal API and are
  only meaningful inside a user turn, where the identity ContextVar is set.

The tools never trust arguments for ownership: ``owner_sub`` comes from the
ContextVar, not the call, so a routine always belongs to the signed-in user.  The
schemas are flat (``time``, ``weekdays`` ...) rather than a nested schedule
object, because a flat schema is what models fill in reliably; this module packs
them into the shape the edge validates.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Settings
from .client import RoutineClient
from .context import get_current
from ..tools.base import Tool

#: The tool names this module owns.  ``tools/executor.py`` routes exactly these
#: here and nothing else, so a stray name cannot reach the edge by accident.
ROUTINE_TOOL_NAMES = {
    "get_current_time",
    "create_routine",
    "list_routines",
    "get_routine",
    "update_routine",
    "pause_routine",
    "resume_routine",
    "delete_routine",
    "get_routine_run_status",
}

_TIME = {"type": "string", "description": "24-hour time like '09:00'"}
_WEEKDAYS = {
    "type": "array",
    "items": {"type": "integer", "minimum": 0, "maximum": 6},
    "description": "weekdays, 0=Sunday .. 6=Saturday",
}


def routine_tools() -> List[Tool]:
    return [
        Tool(
            "get_current_time",
            "The current date and time from the server's clock. Use this instead "
            "of estimating the time. Pass a timezone for the owner's local time.",
            {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone like 'Europe/Paris'; defaults to the owner's timezone",
                    }
                },
            },
        ),
        Tool(
            "create_routine",
            "Schedule a recurring task to run later, even while the assistant is "
            "offline. Give a clear task_prompt describing what to do each time. "
            "One of: schedule_type 'once' with 'at' (ISO 8601 timestamp); 'daily' "
            "with 'time'; 'weekly' with 'time' and 'weekdays'; or 'monthly' with "
            "'time' and 'day'. Times are interpreted in 'timezone'.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short label for the routine"},
                    "task_prompt": {"type": "string", "description": "what to do on each run"},
                    "schedule_type": {"type": "string", "enum": ["once", "daily", "weekly", "monthly"]},
                    "at": {"type": "string", "description": "ISO 8601 timestamp for a one-time routine"},
                    "time": _TIME,
                    "weekdays": _WEEKDAYS,
                    "day": {"type": "integer", "minimum": 1, "maximum": 31, "description": "day of month"},
                    "timezone": {"type": "string", "description": "IANA timezone; defaults to the owner's"},
                },
                "required": ["name", "task_prompt", "schedule_type"],
            },
        ),
        Tool(
            "list_routines",
            "List the routines the owner has scheduled, with their next run.",
            {"type": "object", "properties": {}},
        ),
        Tool(
            "get_routine",
            "Get one routine by id, including its schedule and last run status.",
            {
                "type": "object",
                "properties": {"routine_id": {"type": "string"}},
                "required": ["routine_id"],
            },
        ),
        Tool(
            "update_routine",
            "Change an existing routine's name, task, schedule or timezone.",
            {
                "type": "object",
                "properties": {
                    "routine_id": {"type": "string"},
                    "name": {"type": "string"},
                    "task_prompt": {"type": "string"},
                    "schedule_type": {"type": "string", "enum": ["once", "daily", "weekly", "monthly"]},
                    "at": {"type": "string"},
                    "time": _TIME,
                    "weekdays": _WEEKDAYS,
                    "day": {"type": "integer", "minimum": 1, "maximum": 31},
                    "timezone": {"type": "string"},
                },
                "required": ["routine_id"],
            },
        ),
        Tool(
            "pause_routine",
            "Pause a routine so it starts no new runs until resumed.",
            {
                "type": "object",
                "properties": {"routine_id": {"type": "string"}},
                "required": ["routine_id"],
            },
        ),
        Tool(
            "resume_routine",
            "Resume a paused routine and compute its next run.",
            {
                "type": "object",
                "properties": {"routine_id": {"type": "string"}},
                "required": ["routine_id"],
            },
        ),
        Tool(
            "delete_routine",
            "Delete a routine permanently, including its run history.",
            {
                "type": "object",
                "properties": {"routine_id": {"type": "string"}},
                "required": ["routine_id"],
            },
        ),
        Tool(
            "get_routine_run_status",
            "See the recent runs of a routine and whether they completed, failed "
            "or are still queued.",
            {
                "type": "object",
                "properties": {
                    "routine_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["routine_id"],
            },
        ),
    ]


def _result(output: str = "", error: str = "") -> Dict[str, object]:
    return {"type": "result", "output": output, "error": error}


def _current_time(args: Dict[str, object]) -> Dict[str, object]:
    context = get_current()
    default_tz = (context.timezone if context else "") or "UTC"
    requested = str(args.get("timezone") or "").strip() or default_tz
    try:
        zone = ZoneInfo(requested)
    except (ZoneInfoNotFoundError, ValueError):
        # Fall back to UTC rather than failing: the caller still gets a correct
        # absolute instant, which is the part that matters.
        zone = dt_timezone.utc
        requested = "UTC"
    now = datetime.now(zone)
    offset = now.utcoffset()
    offset_text = "UTC" if not offset else f"UTC{offset.total_seconds() / 3600:+.2f}".rstrip("0").rstrip(".")
    return _result(
        output=(
            f"{now.isoformat()} ({now.strftime('%A')}) in {requested} [{offset_text}]. "
            f"UTC: {now.astimezone(dt_timezone.utc).isoformat()}"
        )
    )


def _pack_schedule(args: Dict[str, object]) -> Optional[Dict[str, object]]:
    """Turn the flat tool fields into the schedule object the edge validates."""
    schedule_type = str(args.get("schedule_type") or "").strip()
    if not schedule_type:
        return None
    definition: Dict[str, object] = {}
    if schedule_type == "once":
        definition["at"] = args.get("at")
    elif schedule_type == "daily":
        definition["time"] = args.get("time")
    elif schedule_type == "weekly":
        definition["time"] = args.get("time")
        definition["weekdays"] = args.get("weekdays")
    elif schedule_type == "monthly":
        definition["time"] = args.get("time")
        definition["day"] = args.get("day")
    return {"schedule_type": schedule_type, "schedule": definition}


def _describe_routine(routine: Dict[str, object]) -> str:
    next_at = routine.get("next_run_at")
    next_text = "none"
    if isinstance(next_at, (int, float)):
        next_text = datetime.fromtimestamp(next_at / 1000, dt_timezone.utc).isoformat()
    return (
        f"[{routine.get('routine_id')}] {routine.get('name')} — "
        f"{routine.get('schedule_text') or routine.get('schedule_type')}; "
        f"timezone {routine.get('timezone')}; "
        f"{'enabled' if routine.get('enabled') else 'paused'}; "
        f"next run {next_text}; last status {routine.get('last_run_status') or 'never run'}"
    )


def _client_or_error(settings: Settings):
    context = get_current()
    if context is None:
        return None, _result(error="routine management needs a signed-in user turn")
    client = RoutineClient.from_settings(settings)
    if client is None:
        return None, _result(error="routine scheduling is not configured (edge origin or token missing)")
    return client, context


def _body_error(body: Dict[str, object]) -> str:
    return str(body.get("error") or "the routine service refused the request")


async def run_routine_tool(name: str, args: Dict[str, object], settings: Settings) -> Dict[str, object]:
    """Execute one routine tool and return a single ``result`` event."""
    if name == "get_current_time":
        return _current_time(args)

    client, context = _client_or_error(settings)
    if client is None:
        return context  # the error event

    caller_owner = context.google_sub

    if name == "create_routine":
        schedule = _pack_schedule(args)
        if schedule is None:
            return _result(error="schedule_type is required")
        response = await client.create(
            {
                "owner_sub": caller_owner,
                "owner_id": context.user_id,
                "agent_id": context.agent_id,
                "name": args.get("name") or "Routine",
                "task_prompt": args.get("task_prompt") or "",
                "timezone": args.get("timezone") or context.timezone or "UTC",
                **schedule,
            }
        )
        body = response["body"]
        if not body.get("ok"):
            hint = " The schedule was ambiguous; ask the user which they meant." if body.get("ambiguous") else ""
            return _result(error=f"{_body_error(body)}.{hint}")
        return _result(output="Created: " + _describe_routine(body.get("routine") or {}))

    if name == "list_routines":
        response = await client.list_for(caller_owner)
        body = response["body"]
        if not body.get("ok"):
            return _result(error=_body_error(body))
        routines = body.get("routines") or []
        if not routines:
            return _result(output="No routines are scheduled.")
        return _result(output="\n".join(_describe_routine(r) for r in routines))

    routine_id = str(args.get("routine_id") or "").strip()
    if not routine_id:
        return _result(error="routine_id is required")

    if name == "get_routine":
        response = await client.get(routine_id, caller_owner)
        body = response["body"]
        if not body.get("ok"):
            return _result(error=_body_error(body))
        return _result(output=_describe_routine(body.get("routine") or {}))

    if name == "update_routine":
        patch: Dict[str, object] = {}
        for key in ("name", "task_prompt", "timezone"):
            if args.get(key):
                patch[key] = args[key]
        schedule = _pack_schedule(args)
        if schedule:
            patch.update(schedule)
        if not patch:
            return _result(error="provide something to change")
        response = await client.update(routine_id, patch, caller_owner)
        body = response["body"]
        if not body.get("ok"):
            return _result(error=_body_error(body))
        return _result(output="Updated: " + _describe_routine(body.get("routine") or {}))

    if name in ("pause_routine", "resume_routine"):
        response = await (client.pause(routine_id, caller_owner) if name == "pause_routine"
                          else client.resume(routine_id, caller_owner))
        body = response["body"]
        if not body.get("ok"):
            return _result(error=_body_error(body))
        verb = "Paused" if name == "pause_routine" else "Resumed"
        return _result(output=f"{verb}: " + _describe_routine(body.get("routine") or {}))

    if name == "delete_routine":
        response = await client.delete(routine_id, caller_owner)
        body = response["body"]
        if not body.get("ok"):
            return _result(error=_body_error(body))
        return _result(output=f"Deleted routine {routine_id}.")

    if name == "get_routine_run_status":
        limit = int(args.get("limit") or 10)
        response = await client.runs(routine_id, caller_owner, limit=limit)
        body = response["body"]
        if not body.get("ok"):
            return _result(error=_body_error(body))
        runs = body.get("runs") or []
        if not runs:
            return _result(output="That routine has not run yet.")
        lines = []
        for run in runs:
            scheduled = run.get("scheduled_for")
            when = (
                datetime.fromtimestamp(scheduled / 1000, dt_timezone.utc).isoformat()
                if isinstance(scheduled, (int, float)) else "?"
            )
            detail = run.get("result_summary") or run.get("error_code") or ""
            lines.append(f"{when} — {run.get('status')} (attempt {run.get('attempt_count')}) {detail}".strip())
        return _result(output="\n".join(lines))

    return _result(error=f"unknown routine tool {name}")
