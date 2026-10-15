"""Recurring routines for ordinary agents.

A routine is a task with a schedule that the *edge* owns: it is stored in D1 by
the Cloudflare Worker and its next occurrence is computed there, because the
schedule has to keep working while this backend -- and the Codespace it runs on
-- is stopped.  The backend's part is to expose the management tools to the
agent and to execute a due task when the edge dispatches it.

The one piece of state that has to travel from a request into a tool call is the
caller's identity.  Tool execution (``tools/executor.py``) has no request object
and never will, so it is carried in a ``ContextVar`` set for the duration of one
agent turn.  That is what lets ``create_routine`` write ``owner_sub`` -- the
stable Google subject the edge keys on -- without the tool argument being able to
forge it.
"""
from .context import RoutineContext, get_current, reset_current, set_current
from .tools import ROUTINE_TOOL_NAMES, routine_tools, run_routine_tool

__all__ = [
    "RoutineContext",
    "get_current",
    "set_current",
    "reset_current",
    "ROUTINE_TOOL_NAMES",
    "routine_tools",
    "run_routine_tool",
]
