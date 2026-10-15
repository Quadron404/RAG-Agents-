"""Execute one due routine.

This is the backend half of "dispatch": the edge decides *when* a routine is due
and calls ``/internal/routines/execute``; this module does the *work* and reports
what actually happened, so the run is only ever recorded complete against a real
result.

A routine is an ordinary, non-computer task: it gets the same worker tools an
ordinary agent gets (browse / fetch / search / files / shell, all restricted),
and it deliberately cannot manage routines itself -- the identity ContextVar is
not set here, so ``create_routine`` and friends refuse.  That keeps routines from
spawning routines with no user in the loop.

Failures are classified, not swallowed: a rate limit or an upstream outage is
reported as retryable so the edge delays the run, while a bad request or an empty
model answer is final.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from ..config import Settings
from ..providers.errors import ModelCallGateTimeout, ProviderHTTPError
from ..providers.router import ProviderUnavailable, Router
from ..tools.executor import Executor
from ..tools.registry import worker_tools
from .commander import OWNER_NOTIFICATION_POLICY
from .loop import run_agent_loop

ROUTINE_SYSTEM_PROMPT = (
    "You are an assistant carrying out a scheduled task that the user set up in "
    "advance. No user is watching right now, so complete the task on your own and "
    "do not ask questions.\n\n"
    "Your tools are: shell, files (read_file/write_file/list_dir), web_search, "
    "fetch_url and a restricted browser (browse) that can only reach sites the "
    "owner has approved. To act, end a sentence with an inline text tool call such "
    "as [TOOL fetch_url {\"url\":\"https://example.com\"}]; the system runs it and "
    "returns the result. You cannot schedule new routines.\n\n"
    + OWNER_NOTIFICATION_POLICY
    + "\n\n"
    "When the task is done, report a concise factual result -- what you found or "
    "did, with numbers, paths or quotes as needed -- and no remaining [TOOL ...] "
    "markers. Do not claim anything you did not actually do."
)


def classify_failure(exc: BaseException) -> Tuple[bool, str]:
    """Map an exception to ``(retryable, error_code)`` for the edge.

    Retryable means "the provider told us to come back later", so the edge should
    delay the run rather than fail it.  Everything else is final.
    """
    if isinstance(exc, ProviderHTTPError):
        if exc.status == 429:
            return True, "rate_limited"
        if 500 <= exc.status <= 599:
            return True, "provider_unavailable"
        return False, "provider_rejected"
    if isinstance(exc, ModelCallGateTimeout):
        return True, "rate_limited"
    if isinstance(exc, ProviderUnavailable):
        return True, "provider_unavailable"
    # A transport error (network blip, timeout) is worth another try; anything
    # else is a bug in the task and retrying only burns quota.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True, "transient_network"
    return False, type(exc).__name__


async def run_routine_task(
    router: Router,
    settings: Settings,
    executor: Executor,
    task: Dict[str, object],
) -> Dict[str, object]:
    """Run the routine's prompt and return ``{"ok", "result"|"error_code"}``.

    Provider exceptions are allowed to propagate: the caller classifies them so
    one place (the route) owns the retryable/terminal decision.
    """
    task_prompt = str(task.get("task_prompt") or "").strip()
    if not task_prompt:
        return {"ok": False, "error_code": "empty_task"}

    provider, model = router.resolve("worker")
    result = await run_agent_loop(
        provider=provider,
        model=model,
        system=ROUTINE_SYSTEM_PROMPT,
        context=[],
        user_text=task_prompt,
        tools=worker_tools(),
        executor=executor,
        emit=None,
        agent_name="routine",
        max_iters=settings.max_iters,
    )
    text = str(result.get("text") or "").strip()
    if not text:
        return {"ok": False, "error_code": "empty_result"}
    return {"ok": True, "result": text}


__all__ = ["ROUTINE_SYSTEM_PROMPT", "classify_failure", "run_routine_task"]
