"""The identity a routine tool call runs under.

``contextvars`` rather than a parameter on every tool, because the agent loop and
the executor are shared by every caller and neither has (or should have) a
request.  The value is set for exactly one agent turn and reset afterwards, so a
concurrent turn on another thread cannot see it: ``ContextVar`` is per-task, and
each turn runs in its own task or thread.

Nothing in here is user-supplied.  ``owner_sub`` is the verified Google subject,
read from the user row the session resolved to -- never from tool arguments --
which is what stops an agent (or a prompt injection) from creating or reading a
routine that belongs to someone else.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RoutineContext:
    """Who is acting, for the duration of one agent turn."""

    user_id: str
    #: The stable Google subject.  This is the routine's owner key at the edge.
    google_sub: str
    #: A stable logical agent identity, so a routine keeps running across
    #: conversations and backend restarts.
    agent_id: str
    #: The conversation the turn belongs to, recorded for provenance.
    conversation_id: str
    #: The owner's IANA timezone, the default for a routine with no explicit one.
    timezone: str


_current: contextvars.ContextVar[Optional[RoutineContext]] = contextvars.ContextVar(
    "rag_routine_context", default=None
)


def set_current(context: RoutineContext) -> contextvars.Token:
    return _current.set(context)


def reset_current(token: contextvars.Token) -> None:
    _current.reset(token)


def get_current() -> Optional[RoutineContext]:
    return _current.get()
