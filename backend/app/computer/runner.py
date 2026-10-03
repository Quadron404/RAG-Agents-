"""The computer-control loop: model -> one tool call -> one real action.

The shape of a turn, and the whole reason the loop was reshaped into it:

  1. send the task, the compact text history, and an image *only if the model
     asked for one last turn*
  2. take exactly one tool call
  3. run it on the real remote computer
  4. record what happened as one line of text

The previous loop rebuilt and resent the entire conversation on every turn, and
attached a fresh screenshot to every one of them whether the model had asked to
see anything or not.  Both costs are paid per request, so a twelve-step run paid
twelve prompts, twelve histories and twelve images -- and the images are the
expensive part by a wide margin.  The model usually did not need them: it needed
to know it had arrived on the page, which one line of text says.

So the screen became a tool.  Nothing attaches an image implicitly, the model
calls `screenshot()` when it needs to look, and the image it gets is dropped the
moment it has been acted on.  What persists is text: one short line per state
change, written by the model, which is what the next request carries.

Nothing here fabricates.  A history line is recorded only when the model wrote
one, a screenshot exists only because the real screen was captured, and the
trace reports the token counts the API reported rather than an estimate of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..providers.base import LLMMessage, TextDelta, ToolCall, ToolCallEvent, image_mime, stream_model
from ..providers.errors import ProviderHTTPError
from ..providers.router import (
    ProviderUnavailable,
    Router,
    computer_model_for,
    computer_providers,
)
from .commands import ALLOWED_TYPES, SCREENSHOT_ACTIONS, Bounds, Command
from .controller import ComputerError, RemoteComputer
from .prompt import REFUSAL_NOTE, RETRY_NOTE, build_prompt, screenshot_note
from .tools import (
    STATE_CHANGING_TOOLS,
    TOOL_NAMES,
    computer_tools,
    parse_arguments,
    tool_to_command,
)

# The only states a run can be in, and the only strings the frontend renders.
STATUS_IDLE = "idle"
STATUS_OBSERVING = "observing"
STATUS_CONTROLLING = "controlling"
STATUS_DONE = "done"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"


def _backoff_delay(
    attempt: int,
    retry_after: Optional[float],
    base: float,
    cap: float,
) -> float:
    """How long to wait before retry ``attempt``.

    The provider's ``Retry-After`` wins when it sent a usable one, because it is
    the only party that knows when its quota resets.  Otherwise this is plain
    exponential backoff, capped so a long outage cannot turn one request into a
    request that sleeps for an hour.

    Full jitter is applied on top: the delay is drawn uniformly from
    ``[0, computed]`` rather than being the computed value itself.  Without it,
    every client that got the same 429 retries on the same schedule, which is how
    a rate limit that was already close to its threshold gets driven over it
    again.  Jitter spreads them out.
    """
    import random

    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), float(cap))
    window = min(float(base) * (2 ** max(0, attempt - 1)), float(cap))
    return random.uniform(0.0, window) if window > 0 else 0.0


@dataclass
class ComputerTurnTrace:
    """Everything about one model call, kept so a failure can be located.

    The event log answers "what did the loop do".  This answers "what was the
    model actually given, and what did it actually say back", which is the only
    way to tell a model that ignored its instructions from a prompt that never
    said them, a request that lost the screenshot, and a parser that mangled a
    perfectly good reply.  Those four look identical from the event log alone.

    The raw reply is stored exactly as it arrived.  It is never replaced by a
    friendly message, because the moment it is, the evidence is gone.
    """

    turn: int
    step: int
    timestamp: float
    provider: str = ""
    model: str = ""
    task: str = ""
    first_turn: bool = False
    attempt: int = 0

    # -- A. the request as constructed
    prompt: str = ""
    prompt_attached: bool = False
    message_count: int = 0
    messages_meta: List[Dict[str, Any]] = field(default_factory=list)
    json_only: bool = False
    #: What the model was given access to this turn.  Recorded per request
    #: because it is the claim the whole loop rests on -- a screenshot the model
    #: was never offered is a screenshot it cannot have reasoned about.
    tools_offered: List[str] = field(default_factory=list)
    #: Why this request is happening.  "step" is the steady state; the others are
    #: the cases where the request is not the ordinary one, and they are
    #: exactly the cases where an image is or is not attached.
    reason: str = "step"
    #: The tool call this request produced, before it was validated.
    tool_call: Dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    tool_result: str = ""
    tool_error: str = ""
    # What the parser will actually accept, which is the truth.  The screenshot
    # list below is narrower: it is what a model may do to something it can see.
    # Reporting only the narrow list would hide that `navigate` is still legal
    # later, and reporting only the wide one would hide the rule the model is
    # being held to.
    allowed_types: List[str] = field(default_factory=list)
    screenshot_types: List[str] = field(default_factory=list)

    # -- B. the image, as it was going to be sent
    screenshot_attached: bool = False
    image: str = ""
    image_meta: Dict[str, Any] = field(default_factory=dict)
    #: The user turn this request ended with, in full.
    user_text: str = ""

    # -- C. the request as actually serialised, which is not the same claim as A
    wire: Dict[str, Any] = field(default_factory=dict)
    #: What the request cost, as the API reported it.  The only honest measure:
    #: a local character count is an estimate of a number the vendor already
    #: told us.  `prompt_tokens` is where a screenshot shows up -- an image is
    #: billed as tokens whether or not the model used it -- so this is the
    #: column that makes "was that screenshot worth it" a question with a
    #: number in the answer.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_reported: bool = False
    #: Images on this request.  Read off the serialised body, so it counts what
    #: went to the API rather than what the loop believed it was sending.
    images_sent: int = 0
    #: The compact text history as it went out, so the trace can show that the
    #: history stayed text: one line per state change, no base64 anywhere.
    history_lines: List[str] = field(default_factory=list)
    history_chars: int = 0

    # -- D. what came back, verbatim
    raw: str = ""
    reply_timestamp: float = 0.0
    # A transport or provider failure, kept separate from a parse failure: the
    # two are indistinguishable from the outside and get "fixed" in the wrong
    # place.
    error: str = ""

    # -- E. what the parser made of it
    parse_ok: bool = False
    parse_error: str = ""
    command: Dict[str, Any] = field(default_factory=dict)

    # -- E2. how the request ended, when it ended badly
    #:
    #: Split out from `error` because "the provider refused" and "the provider
    #: was unreachable" are different diagnoses with different fixes, and a
    #: flattened sentence cannot say which happened.  `provider_reached` is the
    #: whole distinction; the rest is the evidence behind it.
    provider_reached: Optional[bool] = None
    http_status: int = 0
    http_reason: str = ""
    provider_error: str = ""
    provider_error_raw: str = ""
    retry_after: Optional[float] = None
    retry_attempts: int = 0
    simple_task: bool = False
    action_count: int = 0
    screenshot_count: int = 0
    http_attempts: List[Dict[str, Any]] = field(default_factory=list)

    # -- F. what the machine did about it
    execution: Dict[str, Any] = field(default_factory=dict)
    # -- G. the frame that followed, so a before/after pair can be compared
    next_image: str = ""
    next_image_meta: Dict[str, Any] = field(default_factory=dict)

    def public(self, include_images: bool = True) -> Dict[str, Any]:
        """The inspector's view of one turn."""
        out = {
            "turn": self.turn,
            "step": self.step,
            "timestamp": self.timestamp,
            "reply_timestamp": self.reply_timestamp,
            "provider": self.provider,
            "model": self.model,
            "task": self.task,
            "first_turn": self.first_turn,
            "attempt": self.attempt,
            "prompt": self.prompt,
            "prompt_attached": self.prompt_attached,
            "message_count": self.message_count,
            "messages_meta": self.messages_meta,
            "json_only": self.json_only,
            "tools_offered": self.tools_offered,
            "reason": self.reason,
            "tool_call": self.tool_call,
            "tool_call_id": self.tool_call_id,
            "tool_result": self.tool_result,
            "tool_error": self.tool_error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "usage_reported": self.usage_reported,
            "images_sent": self.images_sent,
            "history_lines": self.history_lines,
            "history_chars": self.history_chars,
            "allowed_types": self.allowed_types,
            "screenshot_types": self.screenshot_types,
            "screenshot_attached": self.screenshot_attached,
            "image_meta": self.image_meta,
            "user_text": self.user_text,
            "wire": self.wire,
            "raw": self.raw,
            "error": self.error,
            "provider_reached": self.provider_reached,
            "http_status": self.http_status,
            "http_reason": self.http_reason,
            "provider_error": self.provider_error,
            "provider_error_raw": self.provider_error_raw,
            "retry_after": self.retry_after,
            "retry_attempts": self.retry_attempts,
            "http_attempts": self.http_attempts,
            "parse_ok": self.parse_ok,
            "parse_error": self.parse_error,
            "command": self.command,
            "execution": self.execution,
            "next_image_meta": self.next_image_meta,
        }
        if include_images:
            out["image"] = self.image
            out["next_image"] = self.next_image
        else:
            # Keys stay, values go.  A missing key is ambiguous -- it reads as
            # "no screenshot was sent" -- while an empty one with
            # `image_withheld` beside it is unambiguous.
            out["image"] = ""
            out["next_image"] = ""
        return out


@dataclass
class ComputerEvent:
    """One record of one turn, written to the database.

    Screenshot bytes are deliberately absent.  A dozen base64 JPEGs is several
    megabytes per run; the dimensions and hash are enough to prove afterwards
    which image a coordinate was read from.  The bytes for the inspector live
    in ``ComputerTurnTrace`` instead, which is held in memory only and bounded.
    """

    step: int
    command: Dict[str, Any]
    raw_reply: str
    result: str
    error: str = ""
    screenshot: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class ComputerRun:
    task_id: str
    task: str
    thread_id: str = ""
    status: str = STATUS_IDLE
    step: int = 0
    message: str = ""
    last_url: str = ""
    events: List[ComputerEvent] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancelled: bool = False
    #: Which provider answers the next request.  Empty means the configured
    #: default.  Held on the run rather than passed per call so the selector can
    #: change it while the loop is mid-flight, and so the value the trace shows
    #: is the value the runner used.
    provider: str = ""
    #: Per-turn model I/O.  In memory only, never written to the database: it
    #: holds the screenshot bytes, which are the largest thing in the process
    #: and are only ever alive for the one request that asked for them.
    trace: List[ComputerTurnTrace] = field(default_factory=list)
    #: The compact text history, one short line per state-changing action,
    #: written by the model through `history(note)` and never by this module.
    #:
    #: This is the run's memory, and it is written by the *executor*, not by the
    #: model.  Each line is a fact the machine produced, in the form
    #: ``navigate https://x.com → FAILED: connection timeout`` or
    #: ``click (540,420) → SUCCESS``.  Text-only by construction: nothing in this
    #: loop can put an image here, so "the history contains no screenshots" is a
    #: property of the data structure rather than a promise in a comment.
    #:
    #: It used to be the model's own account of what it had done, which meant a
    #: run could believe it had navigated when the machine had refused, and spend
    #: the rest of its steps building on that.
    facts: List[str] = field(default_factory=list)
    #: The model's optional `history()` lines.  Kept, because a model noticing
    #: something the executor cannot see is worth keeping -- but they are never
    #: proof that anything happened, never gate the next action, and never
    #: appear as fact in the run report.
    history_text: str = ""
    #: What the executor actually did with the most recent action, and what it
    #: actually reported back.  This is the single source of truth the next
    #: request is told about, so the model learns what happened from the machine
    #: rather than from its own memory of what it asked for.
    last_action: Dict[str, Any] = field(default_factory=dict)
    #: The signature of the last call this loop refused, so that an identical
    #: refusal is recognised rather than paid for a second time.
    last_refused: str = ""
    #: The screenshot the model asked for, held for exactly one request.
    #:
    #: Set by `screenshot()` and cleared by the request that carries it.  It
    #: cannot be attached twice, and it is never written into `facts`, so no
    #: later request can inherit an image the loop has already shown.
    pending_image: str = ""
    pending_width: int = 0
    pending_height: int = 0
    pending_screenshot_call_id: str = ""
    #: The size of the last screenshot the model actually saw.  Coordinates are
    #: checked against this, because a coordinate is only meaningful in the grid
    #: it was read from -- and after the image is gone, the grid is the only
    #: record that the model ever saw a screen at all.
    seen_width: int = 0
    seen_height: int = 0
    #: Cumulative token spend, summed from what the API reported per request.
    #: Empty means no provider reported usage, which is reported as such rather
    #: than as zero: a total of zero is a claim, and "nobody told us" is a fact.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests_with_images: int = 0
    #: Every HTTP refusal seen on the current request, oldest first, so the
    #: trace can show that a 429 was retried twice before giving up rather than
    #: only the last one.  Reset per request, not per run: a retry budget is
    #: about one request.
    http_attempts: List[Dict[str, Any]] = field(default_factory=list)
    #: Conservative task classification and counters used to bound simple one-action requests.
    simple_task: bool = False
    action_count: int = 0
    screenshot_count: int = 0

    def public(self) -> Dict[str, Any]:
        """What the browser is allowed to see.

        No raw replies, no coordinates, no image data and nothing derived from
        the API key: the frontend needs to render a status line, not audit the
        run.  The full event list stays server-side.
        """
        return {
            "task_id": self.task_id,

            "status": self.status,
            "step": self.step,
            "message": self.message,
            "url": self.last_url,
            "steps": len(self.events),
            "turns": len(self.trace),
            "running": self.status in (STATUS_OBSERVING, STATUS_CONTROLLING),
            "done": self.status in (STATUS_DONE, STATUS_STOPPED, STATUS_ERROR),
            "stopped": self.status == STATUS_STOPPED,
            # What the run has cost so far, and how much of it involved an
            # image.  A run that finished in four requests is the whole point of
            # the redesign, and this is where that becomes visible to the user
            # without opening the inspector.
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "requests": len(self.trace),
            "screenshot_requests": self.requests_with_images,
        }

    def _failure(self) -> Dict[str, Any]:
        """The structured reason this run stopped, if it stopped badly.

        Gathered from the trace rather than kept separately, so it cannot drift
        from the turn it describes.  Returns ``{}`` for a run that has not
        failed; `trace_report` then omits the key entirely, because an empty
        object is truthy in JavaScript and would read as a failure.
        """
        if self.status != STATUS_ERROR:
            return {}
        for turn in reversed(self.trace):
            if not turn.error:
                continue
            return {
                "turn": turn.turn,
                "provider": turn.provider,
                "model": turn.model,
                "message": turn.error,
                "provider_reached": turn.provider_reached,
                "http_status": turn.http_status,
                "http_reason": turn.http_reason,
                "provider_error": turn.provider_error,
                "retry_after": turn.retry_after,
                "retry_attempts": turn.retry_attempts,
                "http_attempts": list(turn.http_attempts),
                "final_result": "stopped",
            }
        return {}

    def trace_report(
        self,
        include_images: bool = True,
        providers: Optional[List[Any]] = None,
        default_provider: str = "",
    ) -> Dict[str, Any]:
        """The whole run as the inspector and "Copy trace" render it.

        Deliberately built from the same records the loop wrote rather than
        recomputed, so what is displayed cannot disagree with what happened.
        """
        report = self._build_report(include_images, providers, default_provider)
        # `failure` is left out entirely when the run did not fail, rather than
        # sent as an empty object. `{}` is truthy in JavaScript, so a panel that
        # renders on the presence of this key puts a failure banner above a run
        # that finished cleanly -- which is exactly what happened: an "empty"
        # failure with no turn, no provider and no message, displayed as a
        # confident report of a failure that never occurred.
        if not report.get("failure"):
            report.pop("failure", None)
        return report

    def _build_report(
        self,
        include_images: bool,
        providers: Optional[List[Any]],
        default_provider: str,
    ) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "thread_id": self.thread_id,
            "status": self.status,
            "message": self.message,
            "url": self.last_url,
            "step": self.step,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            # The provider the next request will use, so the selector can show
            # the current choice rather than only the one that was last used.
            "provider": self.provider or default_provider,
            # What the browser may know about each selectable provider: name,
            # model, and whether it is configured.  Built by the caller out of
            # Settings and passed in, rather than reaching for settings from
            # here, so the run record never holds the API keys these are
            # derived from.
            "selected_providers": [
                {
                    "name": info.name,
                    "label": info.label,
                    "model": info.model,
                    "configured": info.configured,
                }
                for info in (providers or [])
            ],
            # The model that answered the most recent request, so the panel's
            # header can name it. Taken from the trace rather than settings:
            # after a provider failure the configured model and the model that
            # actually replied are not the same claim.
            "last_provider": self.trace[-1].provider if self.trace else "",
            "last_model": self.trace[-1].model if self.trace else "",
            # Why the run stopped, in a form the panel can render field by
            # field.  `failed_turn` is the last turn that carries an `error`,
            # so a failure is attributed to the request that caused it rather
            # than to whatever happened to be last in the list.
            "failure": self._failure(),
            # The compact text history, as a list of the model's own lines.  It
            # is sent in every request, so it is the second-largest recurring
            # cost after the prompt itself -- and unlike the images it replaced,
            # it grows by one bounded line per action rather than by a whole
            # frame.
            "history": self.history_text,
            # Token accounting, so "the loop got cheaper" is a number rather
            # than an impression.  `usage_reported` is false when no provider
            # sent counts, and the totals are then absent rather than zero.
            "usage": {
                "reported": any(turn.usage_reported for turn in self.trace),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "requests": len(self.trace),
                "requests_with_images": self.requests_with_images,
                "tokens_per_request": (
                    round((self.prompt_tokens + self.completion_tokens) / len(self.trace), 1)
                    if self.trace and any(turn.usage_reported for turn in self.trace)
                    else None
                ),
            },
            "protocol": {
                "tools": list(TOOL_NAMES),
                "state_changing_tools": sorted(STATE_CHANGING_TOOLS),
                "first_turn_allowed": ["navigate", "search", "error", "screenshot"],
                "json_only": False,
                "screenshots_on_request_only": True,
            },
            "turns": [turn.public(include_images=include_images) for turn in self.trace],
        }


def _simple_single_action(task: str) -> bool:
    """Recognize a conservative one-action user request."""
    text = " ".join(str(task or "").strip().lower().split())
    if not text or len(text) > 240:
        return False
    if any(token in text for token in (
        " and ", " then ", " after ", " before ", " verify", " check ",
        " confirm", " make sure", " tell me", " report", " until ",
    )):
        return False
    return bool(re.match(r"^(click|press|type|scroll|hit)\b", text))


class ComputerRunner:
    def __init__(
        self,
        settings: Settings,
        router: Router,
        db: Any = None,
        manager: Any = None,
    ) -> None:
        self.settings = settings
        self.router = router
        self.db = db
        self.computer = RemoteComputer(
            manager,
            settle_ms=settings.computer_settle_ms,
            settle_ms_click=settings.computer_settle_ms_click,
        )
        self._runs: Dict[str, ComputerRun] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # --- run bookkeeping ---------------------------------------------------

    def get(self, task_id: str) -> Optional[ComputerRun]:
        return self._runs.get(task_id)

    async def start(self, task: str, thread_id: str = "", provider: str = "") -> ComputerRun:
        """Queue a run and return it immediately.

        The caller gets a run object rather than a finished result on purpose:
        a loop that had to be awaited would hold the request open for as long as
        the task takes, which is exactly as long as the user watches their own
        browser move without them.

        ``provider`` names which provider answers the *first* request.  It is a
        starting choice rather than a property of the run: the selector can
        change it between turns, and the conversation it is applied to is
        unchanged, because the history is rebuilt from the recorded events
        either way.  Blank means the configured default.
        """
        task_id = uuid.uuid4().hex
        run = ComputerRun(
            task_id=task_id, task=task, thread_id=thread_id, started_at=time.time(),
            simple_task=_simple_single_action(task),
        )
        if provider:
            run.provider = provider
        async with self._lock:
            self._runs[task_id] = run
        self._tasks[task_id] = asyncio.create_task(self._execute(run))
        return run

    def set_provider(self, task_id: str, provider: str) -> ComputerRun:
        """Point the next request at a different provider.

        Takes effect on the next request, not the current one: a model call
        already in flight cannot be recalled, and pretending otherwise would
        mean the trace's provider and the provider that was actually called
        could disagree.  The run, its history, its protocol and its step count
        are all untouched -- only who answers next changes.
        """
        run = self._runs.get(task_id)
        if run is None:
            raise KeyError(task_id)
        run.provider = provider
        return run

    async def stop(self, task_id: str) -> bool:
        run = self._runs.get(task_id)
        if run is None:
            return False
        run.cancelled = True
        task = self._tasks.get(task_id)
        if task and not task.done():
            task.cancel()
        return True

    def providers(self) -> List[Any]:
        """What the browser is allowed to know about each selectable provider."""
        return computer_providers(self.settings)

    def report(self, run: ComputerRun, include_images: bool = True) -> Dict[str, Any]:
        """The trace report for a run, with the provider list filled in."""
        return run.trace_report(
            include_images=include_images,
            providers=computer_providers(self.settings),
            default_provider=self.settings.computer_provider,
        )

    # --- the loop ----------------------------------------------------------

    def _request(
        self,
        run: ComputerRun,
        image: Optional[str],
        note: str,
    ) -> List[LLMMessage]:
        """Build the messages for one request: prompt, task, verified state.

        Two messages, always, in this order.  That is the entire context:

        - the system prompt, which does not vary, and
        - the user's task plus what the executor has actually done, as text.

        There is no third message carrying the previous exchange, no transcript
        of tool calls, and no screenshot unless `image` is set -- and `image` is
        set for exactly one request: the one immediately after the model asked
        for it.

        Everything the loop puts here is a fact the machine produced.  The URL is
        only ever present because `state()` was asked and answered, and the last
        action is reported with the result the executor returned, because a model
        told only what it asked for cannot tell the difference between a click
        that landed and one the agent refused.
        """
        parts = [f"Task: {run.task}"]
        if run.last_url:
            # Only ever set from a verified state read, never from a call the
            # model made or wished it had made.
            parts.append(f"Current URL: {run.last_url}")
        history_text = "\n".join(run.facts)[:12000]
        if history_text:
            parts.append("History.txt (latest complete version; use only this):\n" + history_text)
        if run.last_action:
            parts.append("Last action: " + _result_line(run.last_action))
        if image:
            # Only the current screenshot, stated with its own size so the
            # coordinates that follow are measured in this image's grid.
            parts.append(screenshot_note(run.pending_width, run.pending_height))
        if note:
            parts.append(REFUSAL_NOTE.format(error=note))
        return [
            LLMMessage(role="system", content=build_prompt()),
            LLMMessage(
                role="user",
                content="\n\n".join(parts),
                images=[image] if image else [],
            ),
        ]

    def _request_after_screenshot(
        self,
        run: ComputerRun,
        image: str,
    ) -> List[LLMMessage]:
        """The one request shape that carries an image.

        The screenshot rides on the *user* turn, and the assistant call and tool
        result that explain it stay text-only.  The tool result is not an
        arbitrary choice to move the image: Groq rejects the request outright
        with ``messages[3].content must be a string`` when a tool message's
        content is a content-part list, which is the only way this codebase
        could express an image on a tool result.  So the image goes on the user
        turn, which every OpenAI-compatible endpoint accepts, and the tool
        result says in words what the frame is.

        Exactly one image, on exactly one message.  An earlier version put it on
        both the user turn and the tool result, and every capture was billed
        twice on the single request that carries it.
        """
        messages = self._request(run, image, "")
        call_id = run.pending_screenshot_call_id or "screenshot"
        messages.append(
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=call_id, name="screenshot", arguments="{}")],
            )
        )
        messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=call_id,
                name="screenshot",
                content=f"{run.pending_width}x{run.pending_height} screenshot of the real VM screen",
            )
        )
        return messages

    def _allowed_tools(self, run: ComputerRun) -> List[str]:
        """Narrow the tool catalogue for conservative single-action runs."""
        if not run.simple_task:
            return [name for name in TOOL_NAMES if name != "history"]
        if run.action_count == 0 and not run.seen_width:
            return ["screenshot"]
        return ["click", "type", "key", "scroll", "stop"]

    async def _ask(self, messages: List[LLMMessage], run: ComputerRun) -> Tuple[str, str, str, Dict[str, Any], List[ToolCall], Dict[str, int]]:
        """Make exactly one logical model request.

        The provider layer owns the global ten-second pacing and 429 stall/retry
        policy. A 429 therefore never creates a new logical request here and
        cannot be overtaken by another queued message.
        """
        provider, model = self.router.resolve(
            "computer",
            provider_name=run.provider or None,
        )

        parts: List[str] = []
        calls: List[ToolCall] = []

        async def on_rate_limit(exc: Exception) -> None:
            status = int(getattr(exc, "status", 429) or 429)
            retry_after = getattr(exc, "retry_after", None)
            run.http_attempts.append(
                {
                    "attempt": len(run.http_attempts) + 1,
                    "http_status": status,
                    "http_reason": str(getattr(exc, "reason", "") or ""),
                    "provider_error": str(getattr(exc, "provider_detail", "") or ""),
                    "provider_error_raw": str(getattr(exc, "body", "") or ""),
                    "retry_after": retry_after,
                    "retryable": True,
                }
            )
            if retry_after is None:
                run.message = "Provider rate limit hit — waiting for retry window."
            else:
                run.message = (
                    f"Provider rate limit hit — retrying in {max(0.0, float(retry_after)):.1f}s."
                )

        async def drain() -> None:
            async for event in stream_model(
                provider,
                messages,
                computer_tools(self._allowed_tools(run)),
                model,
                on_rate_limit=on_rate_limit,
            ):
                if isinstance(event, TextDelta):
                    parts.append(event.content)
                elif isinstance(event, ToolCallEvent):
                    calls.append(event.call)

        # There is intentionally no fixed wall-clock timeout here.  A 429 owns
        # the model-call gate until the provider reset time and must not be
        # converted into TimeoutError while it is sleeping.
        await drain()

        wire = getattr(provider, "last_wire", None) or {}
        usage = getattr(provider, "last_usage", None) or {}
        raw = "".join(parts)

        if calls and not raw.strip():
            # A pure native tool-call response has no prose. Keep the trace
            # readable without feeding this synthetic text back to the model.
            raw = "".join(
                json.dumps({"name": c.name, "arguments": c.arguments})
                for c in calls
            )

        return (
            raw,
            getattr(provider, "name", ""),
            model,
            wire,
            calls,
            dict(usage),
        )

    async def _execute(self, run: ComputerRun) -> None:
        """The loop: one tool call per turn, an image only when asked for.

        Three rules do all the work:

        1. No screenshot is captured unless the model called `screenshot()`.  The
           old loop captured one after every single action and attached it to the
           next request, so a run of twelve steps paid for twelve frames whether
           the model looked at them or not.
        2. A screenshot lives for exactly one request.  It is attached to the
           turn that follows the call that asked for it, and dropped after that
turn acts.  It is never written into the facts, so no later request
           can inherit it.
        3. Every action is followed by what the executor reported, and the run
           remembers that.  The model is never obliged to write a line about an
           action before it may take the next one: that obligation cost a whole
           model turn per action, and the line it produced was the model's claim
           rather than the machine's evidence.  The executor records the fact
           itself, so one action is one request.
        """

        run.status = STATUS_OBSERVING
        run.message = "Deciding what to do"

        # `run.step` is the task's step budget, and reading the screen or writing
        # a note deliberately gives a step back -- neither advances the task.  So
        # on its own it cannot bound the loop: a model that asks for one
        # screenshot after another would hold `run.step` at zero and spin here
        # forever, asking for frames it never acts on.  This counts turns
        # instead, including the free ones, so the loop always terminates.
        # The budget is generous because a legitimate run interleaves actions
        # with the screenshots they need, and tight enough that a spiral is a
        # visible stop rather than an unbounded bill.
        max_turns = max(8, self.settings.computer_max_steps * 4)
        turns = 0

        try:
            while run.step < self.settings.computer_max_steps:
                if run.cancelled:
                    run.status = STATUS_ERROR
                    run.message = "Stopped"
                    return
                turns += 1
                if turns > max_turns:
                    run.status = STATUS_ERROR
                    run.message = (
                        f"stopped after {max_turns} turns without advancing: the model "
                        f"kept calling tools that did not move the task forward"
                    )
                    return

                run.status = STATUS_OBSERVING
                run.message = (
                    "Looking at the screen" if run.pending_image else "Deciding what to do"
                )

                command, error = await self._next_command(run)
                if command is None:
                    return  # _next_command already set the terminal state

                turn = run.trace[-1] if run.trace else None
                run.step += 1
                started = time.time()

                if command.type == "screenshot":
                    # Nothing was performed; the screen was read.  Recorded as an
                    # event so the trace shows that a capture happened rather
                    # than a state change, and so the image has an owner.
                    outcome = await self._capture_on_request(run, turn)
                    if outcome is None:
                        return  # capture failed; the terminal state is already set
                    run.step -= 1  # reading the screen is not a step of the task
                    continue

                terminal = await self._perform(run, command)
                if command.type not in ("screenshot", "history", "done", "error") and run.last_action.get("status") == "SUCCESS":
                    run.action_count += 1
                if turn is not None:
                    self._record_execution(turn, command, run, started, terminal)
                if terminal:
                    return

                if run.simple_task and run.last_action.get("status") == "SUCCESS":
                    run.status = STATUS_DONE
                    run.message = "Task complete."
                    return

                if run.last_action.get("status") == "SUCCESS":
                    # Read the machine back only after it said yes.  On a
                    # failure the previous URL is still the truthful one: a
                    # navigation that timed out did not land, and reporting its
                    # target as current is how a run ends up acting on a page it
                    # never reached.
                    try:
                        state = await self.computer.state()
                        verified = str(state.get("url") or "").strip()
                        if verified:
                            run.last_url = verified
                    except ComputerError:
                        pass

            run.status = STATUS_ERROR
            run.message = (
                f"stopped after {self.settings.computer_max_steps} steps without finishing"
            )
        except asyncio.CancelledError:
            if run.cancelled:
                run.status = STATUS_STOPPED
                run.message = "AI has stopped."
            else:
                run.status = STATUS_ERROR
                run.message = "Stopped"
        except Exception as exc:  # a background task must never die silently
            run.status = STATUS_ERROR
            run.message = f"computer control failed: {exc}"
        finally:
            run.finished_at = time.time()
            self._persist(run)

    async def _capture_on_request(
        self, run: ComputerRun, turn: Optional[ComputerTurnTrace]
    ) -> Optional[str]:
        """Capture the real screen because the model asked for it.

        The capture happens *now* and is held on the run for the single request
        that will carry it.  Deferring it to that request would mean holding a
        promise of an image, which on a machine whose display changes is not the
        same thing as the image the model ends up looking at.

        Returns None when the capture failed and the run has been given a
        terminal status.
        """
        try:
            image, width, height = await self.computer.screenshot()
        except ComputerError as exc:
            run.status = STATUS_ERROR
            run.message = str(exc)
            if turn is not None:
                turn.error = str(exc)
            return None
        run.pending_image = image
        run.screenshot_count += 1
        run.pending_width = width
        run.pending_height = height
        run.pending_screenshot_call_id = (turn.tool_call_id if turn else "") or "screenshot"
        run.seen_width = width
        run.seen_height = height
        # The image the *next* request will carry, stored so the inspector can
        # show the frame the model actually saw rather than one that looks like
        # it.
        if turn is not None:
            turn.next_image = image
            turn.next_image_meta = _image_meta(image, width, height)
        self._record(
            run,
            {"type": "screenshot"},
            "",
            "ok",
            trace=_image_meta(image, width, height),
        )
        return image

    def _record_execution(
        self,
        turn: ComputerTurnTrace,
        command: Command,
        run: ComputerRun,
        started: float,
        terminal: bool,
    ) -> None:
        """Attach what the machine did about a call to the turn that asked."""
        last_event = run.events[-1] if run.events else None
        pointer = dict(last_event.screenshot) if last_event else {}
        result = last_event.result if last_event else ""
        # `done` and `error` record themselves as the run's final message rather
        # than as "ok", so a plain result check would report a successful,
        # deliberate finish as a failure.
        acted = bool(last_event) and (result == "ok" or terminal)
        if command.type == "done" and terminal:
            outcome = "done"
        elif command.type in ("error", "stop") and terminal:
            outcome = "stopped"
        elif result == "ok":
            outcome = "executed"
        elif result == "failed":
            outcome = "refused"
        else:
            outcome = "not_run"
        turn.execution = {
            "accepted": acted,
            "executed": acted,
            "outcome": outcome,
            "command": command.to_json(),
            "result": result,
            "error": last_event.error if last_event else "",
            "duration_ms": round((time.time() - started) * 1000, 1),
            "terminal": terminal,
            "x": pointer.get(f"{command.type}_executed_x"),
            "y": pointer.get(f"{command.type}_executed_y"),
            "actual_pointer_x": pointer.get(f"{command.type}_actual_x"),
            "actual_pointer_y": pointer.get(f"{command.type}_actual_y"),
            "landed": pointer.get(f"{command.type}_landed"),
            "screen_width": pointer.get("screen_width"),
            "screen_height": pointer.get("screen_height"),
        }

    async def _next_command(self, run: ComputerRun):
        """Ask for one tool call, refusing a bad one a bounded number of times.

        Returns ``(command, error)``.  A ``None`` command means the run has
        already been given a terminal status and the caller must stop.  The
        bounds are in `_refuse`: a retry counter for a model that is correcting
        itself, and a call-signature check for one that is not reading the error
        at all.
        """
        refusal = ""
        for attempt in range(self.settings.computer_max_json_retries + 2):
            # The retry budget belongs to one request.  Cleared here so the
            # attempts recorded on this turn are this request's, not a leftover
            # count from whatever failed earlier in the run.
            run.http_attempts = []
            # One trace entry per *request*.  A retry is a second real call to
            # the model with a different request, and the question the inspector
            # has to answer is "what did the API receive, and what came back" --
            # so a retry that overwrote the first attempt's reply would erase the
            # very reply that caused the retry.
            turn = ComputerTurnTrace(
                turn=len(run.trace) + 1,
                step=run.step + 1,
                timestamp=time.time(),
                task=run.task,
                attempt=attempt,
                allowed_types=list(ALLOWED_TYPES),
                screenshot_types=list(SCREENSHOT_ACTIONS),
                tools_offered=self._allowed_tools(run),
                json_only=False,
                # The reason this request exists decides whether it carries an
                # image, so it is recorded before the call rather than inferred
                # afterwards from whatever was attached.
                reason="screenshot_result" if run.pending_image else "step",
                history_lines=list(run.facts),
                history_chars=sum(len(n) for n in run.facts),
            )
            run.trace.append(turn)
            self._trim_trace(run)

            if run.pending_image:
                messages = self._request_after_screenshot(run, run.pending_image)
            else:
                messages = self._request(run, None, refusal)
            if attempt and refusal:
                # The recovery nudge rides on the user turn it corrects, never
                # as a message of its own, for the same reason the correction
                # itself does: two consecutive user messages are rejected by
                # some OpenAI-compatible endpoints.
                messages[-1] = LLMMessage(
                    role="user",
                    content=f"{messages[-1].content}\n\n{RETRY_NOTE}",
                    images=list(messages[-1].images),
                )

            system = next((m for m in messages if m.role == "system"), None)
            turn.prompt = system.content if system else ""
            turn.prompt_attached = system is not None

            turn.message_count = len(messages)
            turn.messages_meta = [
                {
                    "role": m.role,
                    "chars": len(m.content or ""),
                    "images": len(m.images or []),
                    "image_bytes": sum(len(i) for i in (m.images or [])),
                    "content_preview": (m.content or "")[:400],
                }
                for m in messages
            ]
            # What is about to go on the wire, counted before the call so a
            # failed request still shows what it would have cost.
            image_on_wire = any(m.images for m in messages)
            turn.screenshot_attached = image_on_wire
            turn.image = run.pending_image if image_on_wire else ""
            turn.image_meta = (
                _image_meta(run.pending_image, run.pending_width, run.pending_height)
                if image_on_wire and run.pending_image
                else {}
            )
            last_user = next((m for m in reversed(messages) if m.role == "user"), None)
            turn.user_text = last_user.content if last_user else ""

            raw = ""
            provider_name = ""
            model = ""
            wire: Dict[str, Any] = {}
            calls: List[ToolCall] = []
            usage: Dict[str, int] = {}
            try:
                (
                    raw,
                    provider_name,
                    model,
                    wire,
                    calls,
                    usage,
                ) = await self._ask(messages, run)
            except ProviderUnavailable as exc:
                # The provider is named even though the call never happened, so
                # the trace shows which one was selected and refused.  Blanking
                # it made "Mistral is not configured" render as an unattributed
                # error, which is the opposite of a diagnosis.
                turn.provider = run.provider
                turn.model = computer_model_for(self.settings, run.provider) if run.provider else ""
                turn.raw = ""
                turn.reply_timestamp = time.time()
                turn.error = str(exc)
                turn.provider_reached = False
                run.status = STATUS_ERROR
                run.message = str(exc)
                return None, str(exc)
            except ProviderHTTPError as exc:
                # The provider was reached and refused.  Recorded as structured
                # fields, not flattened into one string, because the reader has
                # to be able to tell a rate limit from a bad key from a 500.
                turn.provider = exc.provider or run.provider
                turn.model = exc.model or (
                    computer_model_for(self.settings, run.provider) if run.provider else ""
                )
                turn.raw = ""
                turn.reply_timestamp = time.time()
                turn.error = exc.message
                turn.provider_reached = True
                turn.http_status = exc.status
                turn.http_reason = exc.reason
                turn.provider_error = exc.provider_detail
                turn.provider_error_raw = exc.body
                turn.retry_after = exc.retry_after
                turn.retry_attempts = len(run.http_attempts or [])
                turn.http_attempts = list(run.http_attempts or [])
                run.status = STATUS_ERROR
                run.message = exc.message
                return None, exc.message
            except Exception as exc:
                # Anything else: a transport failure, a timeout, a bug.  The
                # type is named because "something went wrong" is what made this
                # path hard to fix.
                turn.provider = run.provider
                turn.model = computer_model_for(self.settings, run.provider) if run.provider else ""
                turn.raw = ""
                turn.reply_timestamp = time.time()
                turn.error = f"{type(exc).__name__}: {exc}"
                turn.provider_reached = False
                run.status = STATUS_ERROR
                run.message = turn.error
                return None, turn.error

            turn.provider = provider_name
            turn.model = model
            turn.wire = wire
            turn.reply_timestamp = time.time()
            # Refusals this request recovered from, carried on success too: a
            # request that was refused once and then answered looks identical to
            # one that was never refused.
            turn.http_attempts = list(run.http_attempts or [])
            turn.retry_attempts = len(turn.http_attempts)
            if turn.http_attempts:
                last_refusal = turn.http_attempts[-1]
                turn.http_status = int(last_refusal.get("http_status") or 0)
                turn.http_reason = str(last_refusal.get("http_reason") or "")
                turn.provider_error = str(last_refusal.get("provider_error") or "")
                turn.provider_error_raw = str(last_refusal.get("provider_error_raw") or "")
                turn.retry_after = last_refusal.get("retry_after")
                turn.provider_reached = True
            # Verbatim.  Whatever came back is what gets shown, including prose.
            turn.raw = raw

            # Token accounting, from what the API reported and counted off the
            # serialised body rather than from application state.
            turn.prompt_tokens = int(usage.get("prompt_tokens") or 0)
            turn.completion_tokens = int(usage.get("completion_tokens") or 0)
            turn.total_tokens = int(usage.get("total_tokens") or 0)
            turn.usage_reported = bool(usage)
            turn.images_sent = int(wire.get("image_count") or 0) if wire else 0
            run.prompt_tokens += turn.prompt_tokens
            run.completion_tokens += turn.completion_tokens
            if turn.images_sent:
                run.requests_with_images += 1

            # Keep the image alive through a no-tool/malformed-tool recovery once.
            # This avoids the wasteful failure pattern:
            # screenshot -> empty response -> screenshot again -> click.
            # A recovery gets the same frame rather than asking the model to pay for
            # a second screenshot capture and an extra request.
            if not calls and attempt >= 1:
                run.pending_image = ""
                run.pending_screenshot_call_id = ""

            if not calls and not raw.strip():
                refusal = "the model returned an empty response (no text and no tool call)"
                turn.parse_error = refusal
                if self._refuse(run, turn, raw, refusal, "<empty-response>", attempt,
                                "the model returned an empty response"):
                    continue
                return None, refusal

            if not calls:
                refusal = "no tool was called; call exactly one tool now"
                turn.parse_error = refusal
                if self._refuse(run, turn, raw, refusal, "<no-call>", attempt,
                                "the model stopped calling tools"):
                    continue
                return None, refusal

            if len(calls) > 1:
                # Sequential by contract.  Two calls in one reply means the model
                # is trying to act on a state it has not observed yet, which is
                # how a click lands on a page that has not finished loading.
                refusal = (
                    f"{len(calls)} tool calls in one reply; call one tool at a time "
                    f"so each action is seen before the next"
                )
                turn.parse_error = refusal
                if self._refuse(run, turn, raw, refusal, "<multi-call>", attempt,
                                "the model would not act one step at a time"):
                    continue
                return None, refusal

            call = calls[0]
            allowed_now = self._allowed_tools(run)
            if call.name not in allowed_now:
                refusal = f"{call.name} is not allowed for this task state; allowed now: {', '.join(allowed_now)}"
                turn.tool_error = refusal
                if self._refuse(run, turn, raw, refusal, f"{call.name}:{call.arguments}", attempt, "the model selected a tool that is not allowed now"):
                    continue
                return None, refusal
            turn.tool_call_id = call.id
            turn.tool_call = {"name": call.name, "arguments": call.arguments}
            signature = f"{call.name}:{call.arguments}"
            args = parse_arguments(call.arguments)
            if args is None:
                refusal = f"the arguments of {call.name}() were not valid JSON"
                turn.tool_error = refusal
                if self._refuse(run, turn, raw, refusal, signature, attempt,
                                "the model sent unusable arguments"):
                    continue
                return None, refusal

            if call.name == "click" and not run.seen_width:
                # A click needs a frame to be a coordinate in.  Refusing it is
                # not pedantry: a coordinate chosen without having seen anything
                # lands on whatever happens to occupy that pixel, which on a real
                # user's browser is a button nobody intended to press.
                refusal = (
                    "you have not seen the screen yet; call screenshot() and read "
                    "it before clicking"
                )
                turn.tool_error = refusal
                if self._refuse(run, turn, raw, refusal, signature, attempt,
                                "the model would not look before clicking"):
                    continue
                return None, refusal

            bounds = (
                Bounds(width=run.seen_width, height=run.seen_height)
                if run.seen_width and run.seen_height
                else None
            )
            command, error = tool_to_command(call.name, args, bounds)
            if command is None:
                turn.tool_error = error
                refusal = error
                if self._refuse(run, turn, raw, error, signature, attempt,
                                "the model never issued a usable call"):
                    continue
                return None, error

            if run.simple_task and command.type not in ("screenshot", "done", "error") and run.action_count >= 1:
                refusal = "simple task already has its one allowed action; finish the task"
                turn.tool_error = refusal
                run.status = STATUS_ERROR
                run.message = refusal
                return None, refusal

            turn.parse_ok = True
            turn.command = command.to_json()
            turn.tool_result = command.to_json()
            # The image is spent exactly once after we have accepted a real
            # command. Empty/malformed recovery is the only path allowed to reuse
            # it, and only for one retry.
            run.pending_image = ""
            run.pending_screenshot_call_id = ""
            refusal = ""
            run.last_refused = ""
            return command, ""

        run.status = STATUS_ERROR
        run.message = "the model never issued a usable call"
        return None, ""

    def _refuse(
        self,
        run: ComputerRun,
        turn: ComputerTurnTrace,
        raw: str,
        error: str,
        signature: str,
        attempt: int,
        give_up: str,
    ) -> bool:
        """Refuse one unusable call.  Returns True when the model should try again.

        A refusal is given once, with the reason, and then the model is asked for
        a corrected call -- it is never the same call again.  Two separate bounds
        enforce that, and both are needed:

        - The retry counter bounds a model that is correcting itself, alternating
          between two bad calls, or drifting.
        - The signature check bounds a model that is not reading the error at
          all.  When the identical unusable call comes back a second time, asking
          a third time cannot help: it only spends tokens to arrive at the same
          place.  So it stops immediately, and says what has to change.

        The second is what stops
        ``invalid → refusal → invalid → refusal → invalid`` from running to the
        step limit, and it fires two attempts earlier than the counter would.
        """
        self._record(run, {"type": "invalid"}, raw, "refused", error=error)
        if signature and signature == run.last_refused:
            turn.parse_error = (
                f"{error} (the same unusable call was refused twice, so the run "
                f"stopped instead of spending another request on it)"
            )
            run.status = STATUS_ERROR
            run.message = f"{give_up}: {error}"
            return False
        run.last_refused = signature
        if attempt >= self.settings.computer_max_json_retries:
            run.status = STATUS_ERROR
            run.message = f"{give_up}: {error}"
            return False
        return True

    def _trim_trace(self, run: ComputerRun) -> None:
        """Keep the screenshots of the most recent turns only.

        The trace holds real base64 JPEGs, which are a few hundred kilobytes
        each, and a run is capped at ``computer_max_steps`` turns -- so without
        this a long run would quietly pin tens of megabytes in the backend for
        as long as the run object is alive.  The metadata of a dropped image is
        kept, so the trace still shows that a screenshot existed and what size
        it was; only the bytes go.
        """
        limit = max(2, int(self.settings.computer_max_steps))
        for old in run.trace[:-limit] if len(run.trace) > limit else []:
            old.image = ""
            old.next_image = ""

    async def _perform(self, run: ComputerRun, command: Command) -> bool:
        """Execute one command.  The only place a command reaches the machine.

        This is the only writer of `run.last_action` and `run.facts`, which is
        the whole point: the run's memory of what happened is produced here, by
        the code that can see whether it happened, and not by the model that
        asked.  A caller cannot learn that an action succeeded except by reading
        what this method recorded.

        Returns True when the run has reached a terminal state.
        """
        if command.type == "done":
            run.status = STATUS_DONE
            run.message = command.message or "Task complete."
            run.last_action = {"tool": "done", "status": "SUCCESS", "detail": ""}
            self._record(run, command.to_json(), "", run.message)
            return True
        if command.type == "error":
            run.status = STATUS_ERROR
            run.message = command.message or "The model reported it could not continue."
            run.last_action = {"tool": "error", "status": "SUCCESS", "detail": ""}
            self._record(run, command.to_json(), "", "stopped")
            return True
        if command.type == "stop":
            run.status = STATUS_STOPPED
            run.message = command.message or "AI has stopped."
            run.last_action = {"tool": "stop", "status": "SUCCESS", "detail": ""}
            self._record(run, command.to_json(), "", "stopped")
            return True

        run.status = STATUS_CONTROLLING
        run.message = {
            "navigate": "Opening a page",
            "search": "Searching",
            "click": "Clicking",
            "type": "Typing",
            "key": "Pressing a key",
            "scroll": "Scrolling",
            "move": "Moving the cursor",
        }[command.type]

        trace: Dict[str, Any] = {}
        try:
            if command.type == "navigate":
                await self.computer.navigate(command.url)
            elif command.type == "search":
                await self.computer.search(command.query)
            elif command.type == "click":
                result = await self.computer.click(command.x, command.y)
                trace = _pointer_trace("click", command.x, command.y, result)
            elif command.type == "type":
                await self.computer.type_text(command.text)
            elif command.type == "key":
                await self.computer.key(command.key)
            elif command.type == "scroll":
                await self.computer.scroll(command.delta_y)
            elif command.type == "move":
                result = await self.computer.move(command.x, command.y)
                trace = _pointer_trace("move", command.x, command.y, result)
            else:
                # Unreachable while parse_command and this dispatch agree, and
                # kept explicit anyway: a `move` fallback here would silently
                # turn a future action into a cursor movement.
                raise ComputerError(f"cannot perform {command.type!r}")
        except ComputerError as exc:
            # The action was refused or failed.  Reported as a normal event so
            # the model sees the evidence and can recover, rather than the run
            # dying on a transient click that landed on a moving page.
            detail = str(exc)
            run.last_action = {"tool": command.type, "status": "FAILED", "detail": detail}
            self._remember(run, command, "FAILED", detail)
            self._record(run, command.to_json(), "", "failed", error=detail)
            run.message = detail
            # The refusal is reported here and the caller in `_loop` turns the
            # recorded event into the turn's execution block, so it cannot be
            # lost just because it failed.
            return False

        run.last_action = {"tool": command.type, "status": "SUCCESS", "detail": ""}
        self._remember(run, command, "SUCCESS", "")
        self._record(run, command.to_json(), "", "ok", trace=trace)
        return False

    def _remember(self, run: ComputerRun, command: Command, status: str, detail: str) -> None:
        """Write one executor-produced fact, and nothing else.

        The line is built from the command the executor was handed and the
        status it returned, so it cannot drift from what was actually attempted.
        `navigate https://x.com → FAILED: connection timeout` is the whole
        memory the next request gets of that action.
        """
        line = f"{_fact_line(command)} → {status}"
        if detail:
            line += f": {detail}"
        run.facts.append(line)

    def _record(
        self,
        run: ComputerRun,
        command: Dict[str, Any],
        raw_reply: str,
        result: str,
        error: str = "",
        trace: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = ComputerEvent(
                step=run.step,
                command=command,
                raw_reply=raw_reply,
                result=result,
                error=error,
                timestamp=time.time(),
            )
        if trace:
            # Kept on the event so a bad click can be diagnosed after the fact:
            # what was asked for, what was sent, and where the pointer ended up.
            event.screenshot = dict(event.screenshot, **trace)
        run.events.append(event)

    def _persist(self, run: ComputerRun) -> None:
        if self.db is None:
            return
        try:
            self.db.save_computer_run(
                run.thread_id,
                run.task_id,
                run.public(),
                [
                    {
                        "step": e.step,
                        "command": e.command,
                        "result": e.result,
                        "error": e.error,
                        "screenshot": e.screenshot,
                        "timestamp": e.timestamp,
                    }
                    for e in run.events
                ],
            )
        except Exception:
            # A log write failing must not change what happened on screen.
            pass


def _fact_line(command: Command) -> str:
    """One action as the executor saw it, in as few tokens as are still exact.

    Only the arguments that identify the action appear, because this line is the
    run's memory and a run of twenty actions must not grow a transcript.  Typed
    text is clipped so a pasted paragraph cannot dominate every later request.
    """
    kind = command.type
    if kind == "navigate":
        return f"navigate {command.url}"
    if kind == "search":
        return f"search {command.query!r}"
    if kind == "click":
        return f"click ({_coord(command.x)},{_coord(command.y)})"
    if kind == "move":
        return f"move ({_coord(command.x)},{_coord(command.y)})"
    if kind == "type":
        return f"type {command.text[:40]!r}"
    if kind == "key":
        return f"key {command.key}"
    if kind == "scroll":
        return f"scroll {command.delta_y}"
    return kind


def _coord(value: float) -> str:
    """A coordinate in the form the model chose it, not in binary-float form.

    A click at (540, 420) is written `540.0,420.0` by Python, which is two extra
    characters on every line of the run's memory and reads like a different
    number from the one the model used.  Integral coordinates print as integers;
    a genuinely fractional one keeps its fraction, because dropping it would
    round a coordinate the model actually chose.
    """
    return str(int(value)) if float(value).is_integer() else str(value)


def _result_line(action: Dict[str, Any]) -> str:
    """The executor's verdict on the last action, as the model is told.

    This is what the model reads instead of its own memory of asking.  The
    detail is the error the machine raised, verbatim, so a refused click reads
    as refused rather than as something the model has to infer from silence.
    """
    tool = str(action.get("tool") or "?")
    status = str(action.get("status") or "SUCCESS")
    detail = str(action.get("detail") or "").strip()
    return f"{tool} → {status}" + (f": {detail}" if detail else "")


def _pointer_trace(kind: str, model_x: float, model_y: float, result: Dict[str, Any]) -> Dict[str, Any]:
    """Every stage of one coordinate, as the agent reported it.

    The whole point of the 1:1 contract is that these three agree.  Recording
    them next to the event means a run that missed its target can be read back
    without re-running it: if model and executed differ the parse was wrong, and
    if executed and actual differ the pointer moved on its own.
    """
    if not isinstance(result, dict):
        # Diagnostics must never be the reason a run dies.
        return {"screen_width": None, "screen_height": None}
    return {
        f"{kind}_model_x": model_x,
        f"{kind}_model_y": model_y,
        f"{kind}_executed_x": result.get("x"),
        f"{kind}_executed_y": result.get("y"),
        f"{kind}_actual_x": result.get("actual_x"),
        f"{kind}_actual_y": result.get("actual_y"),
        f"{kind}_landed": result.get("landed"),
        "screen_width": result.get("display_width"),
        "screen_height": result.get("display_height"),
    }


def _describe_command(command: Dict[str, Any], error: str) -> str:
    """The command, written back to the model as the record of what was issued.

    The model reads this on the next turn, so it is the same JSON it produced and
    it has to survive quoting.  Typed text is arbitrary -- a quote, a backslash
    or a newline in it used to produce a broken fragment that the model then had
    to guess at, so every string goes through the JSON encoder.

    Text is truncated in this description only: the log keeps the whole thing,
    but a screenshot's worth of conversation should not be spent echoing a
    paragraph the model just sent.
    """
    kind = command.get("type", "?")
    if error:
        return f"{{rejected: {error}}}"
    if kind in ("click", "move"):
        return json.dumps(
            {"type": kind, "x": command.get("x"), "y": command.get("y")}
        )
    if kind == "scroll":
        return json.dumps({"type": "scroll", "delta_y": command.get("delta_y")})
    if kind == "type":
        text = str(command.get("text", ""))
        if len(text) > 120:
            text = text[:120] + "..."
        return json.dumps({"type": "type", "text": text})
    if kind == "navigate":
        return json.dumps({"type": "navigate", "url": command.get("url")})
    if kind == "search":
        return json.dumps({"type": "search", "query": command.get("query")})
    if kind == "key":
        return json.dumps({"type": "key", "key": command.get("key")})
    return json.dumps({"type": kind, "message": command.get("message", "")})


def _image_meta(image: str, width: int, height: int) -> Dict[str, Any]:
    """Describe the screenshot without copying it a second time.

    The hash is the useful part: it is what lets a screenshot that was dropped
    for being old still be proven to be the same bytes, and it is what makes
    "the model saw a new frame" a checkable claim rather than an assumption.
    """
    digest = hashlib.sha256(image.encode("ascii", "ignore")).hexdigest()[:16]
    return {
        "width": width,
        "height": height,
        "mime": image_mime(image),
        "bytes_b64": len(image),
        "sha256_16": digest,
    }
