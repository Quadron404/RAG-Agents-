"""The computer-control loop: model -> command -> remote action -> screenshot.

One task is one turn-by-turn exchange.  The shape of a turn is fixed:

  1. send the full conversation + task + state (+ the screenshot captured last turn)
  2. parse the reply into a command, refusing anything off the allowlist
  3. perform exactly one action on the remote computer
  4. capture a fresh screenshot for the next turn

Every turn sends the *complete* history rather than a running summary.  A
control loop that trims its context will eventually forget which page it is on
and click the wrong thing, and the failure is silent.

Screenshots are not written to disk and are not put in the message log.  They
are re-sent from the live display each turn, so what the model sees is always
what the user sees, and the database keeps only enough metadata to audit a run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..providers.base import LLMMessage, TextDelta, image_mime
from ..providers.router import ProviderUnavailable, Router
from .commands import ALLOWED_TYPES, SCREENSHOT_ACTIONS, Bounds, Command, parse_command
from .controller import ComputerError, RemoteComputer
from .prompt import FORMAT_CORRECTION, build_prompt

# The only states a run can be in, and the only strings the frontend renders.
STATUS_IDLE = "idle"
STATUS_OBSERVING = "observing"
STATUS_CONTROLLING = "controlling"
STATUS_DONE = "done"
STATUS_ERROR = "error"


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

    # -- C. the request as actually serialised, which is not the same claim as A
    wire: Dict[str, Any] = field(default_factory=dict)

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
            "allowed_types": self.allowed_types,
            "screenshot_types": self.screenshot_types,
            "screenshot_attached": self.screenshot_attached,
            "image_meta": self.image_meta,
            "wire": self.wire,
            "raw": self.raw,
            "error": self.error,
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
    #: Per-turn model I/O.  In memory only, never written to the database: it
    #: holds the screenshot bytes, which are the largest thing in the process
    #: and are already being re-captured from the display every turn.
    trace: List[ComputerTurnTrace] = field(default_factory=list)

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
            "done": self.status in (STATUS_DONE, STATUS_ERROR),
        }

    def trace_report(self, include_images: bool = True) -> Dict[str, Any]:
        """The whole run as the inspector and "Copy trace" render it.

        Deliberately built from the same records the loop wrote rather than
        recomputed, so what is displayed cannot disagree with what happened.
        """
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
            "protocol": {
                "first_turn_allowed": ["navigate", "search"],
                "after_screenshot_allowed": list(ALLOWED_TYPES),
                "after_screenshot_visible_target": list(SCREENSHOT_ACTIONS),
                "json_only": True,
            },
            "turns": [turn.public(include_images=include_images) for turn in self.trace],
        }


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

    async def start(self, task: str, thread_id: str = "") -> ComputerRun:
        """Queue a run and return it immediately.

        The caller gets a run object rather than a finished result on purpose:
        a loop that had to be awaited would hold the request open for as long as
        the task takes, which is exactly as long as the user watches their own
        browser move without them.
        """
        task_id = uuid.uuid4().hex
        run = ComputerRun(
            task_id=task_id, task=task, thread_id=thread_id, started_at=time.time()
        )
        async with self._lock:
            self._runs[task_id] = run
        self._tasks[task_id] = asyncio.create_task(self._execute(run))
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

    # --- the loop ----------------------------------------------------------

    def _history(self, run: ComputerRun, width: int = 0, height: int = 0) -> List[LLMMessage]:
        """The canonical history, rebuilt from the recorded events.

        Rebuilt rather than carried in a provider-side conversation id, so the
        loop works against any OpenAI-compatible endpoint and the log is the
        authoritative record rather than something only the provider has.

        The system prompt is built for the size of the screen actually captured.
        A model told a fixed 1365x768 while it is looking at a different-sized
        image is being handed a coordinate system that does not exist, and the
        clicks that follow are wrong by a ratio.
        """
        messages = [
            LLMMessage(
                role="system",
                content=build_prompt(width or 1365, height or 768),
            ),
            LLMMessage(role="user", content=f"Task: {run.task}"),
        ]
        for event in run.events:
            messages.append(
                LLMMessage(role="assistant", content=_describe_command(event.command, event.error))
            )
            messages.append(
                LLMMessage(
                    role="user",
                    content=(
                        f"Computer control state:\n"
                        f"result: {event.result}\n"
                        f"current url: {run.last_url or '(unknown)'}"
                    ),
                )
            )
        return messages

    async def _ask(self, messages: List[LLMMessage]) -> Tuple[str, str, str, Dict[str, Any]]:
        """One model call, returning the reply as text.

        Returns ``(text, provider, model, wire)``.  The wire summary is read back
        off the provider after the call, which is the only place that knows what
        the request looked like once it had been serialised.

        Goes through Provider.stream(), which is the only method every provider
        actually implements.  This used to call a non-existent ``provider.chat()``
        and raise AttributeError on the first turn of every run, so the loop
        never once reached a command: the test double had grown its own ``chat``
        that no real provider has, and the suite passed against an interface that
        did not exist outside the tests.  Streaming is also what the OpenRouter
        endpoint is already used for, so this changes no wire behaviour.

        Tool calls are ignored on purpose.  Computer control speaks the JSON text
        protocol only, and a model that also emitted a tool call would have
        nothing to execute it with.
        """
        provider, model = self.router.resolve("computer")
        parts: List[str] = []

        async def drain() -> None:
            async for event in provider.stream(messages, [], model):
                if isinstance(event, TextDelta):
                    parts.append(event.content)

        await asyncio.wait_for(drain(), timeout=120.0)
        wire = getattr(provider, "last_wire", None) or {}
        return "".join(parts), getattr(provider, "name", ""), model, wire

    async def _execute(self, run: ComputerRun) -> None:
        run.status = STATUS_OBSERVING
        run.message = "Looking at the browser"
        # None means "no screenshot has ever been taken", which is exactly the
        # first-turn condition: navigate or search only.
        bounds: Optional[Bounds] = None
        pending_image: Optional[str] = None

        try:
            while run.step < self.settings.computer_max_steps:
                if run.cancelled:
                    run.status = STATUS_ERROR
                    run.message = "Stopped"
                    return

                first_turn = bounds is None
                run.status = STATUS_OBSERVING
                run.message = "Looking at the screen" if not first_turn else "Deciding where to go"

                command, error, raw = await self._next_command(
                    run, bounds, pending_image, first_turn
                )
                if command is None:
                    return  # _next_command already set the terminal state
                # The turn that produced this command, so execution and outcome
                # land on the same record as the reply that caused them.
                turn = run.trace[-1] if run.trace else None

                run.step += 1
                started = time.time()
                terminal = await self._perform(run, command)
                if turn is not None:
                    last_event = run.events[-1] if run.events else None
                    pointer = dict(last_event.screenshot) if last_event else {}
                    result = last_event.result if last_event else ""
                    # `done` and `error` record themselves as the run's final
                    # message rather than as "ok", so a plain result check would
                    # report a successful, deliberate finish as a failure.
                    acted = bool(last_event) and (result == "ok" or terminal)
                    if command.type == "done" and terminal:
                        outcome = "done"
                    elif command.type == "error" and terminal:
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
                if terminal:
                    return

                # A fresh screenshot after every action.  It becomes both the
                # image for the next turn and the bounds any click must fall
                # inside, which is what makes stale coordinates unusable.
                try:
                    image, width, height = await self.computer.screenshot()
                except ComputerError as exc:
                    run.status = STATUS_ERROR
                    run.message = str(exc)
                    return
                pending_image = image
                bounds = Bounds(width=width, height=height)
                if turn is not None:
                    # The image the *next* turn will be sent.  Stored so the
                    # inspector can show the before/after pair, which is the only
                    # way to see whether a click changed the page.
                    turn.next_image = image
                    turn.next_image_meta = _image_meta(image, width, height)
                if run.events:
                    # Merged, not replaced: the event may already be carrying the
                    # pointer trace from the action this screenshot follows, and
                    # that is the evidence for whether the click landed.
                    run.events[-1].screenshot = {
                        **run.events[-1].screenshot,
                        **_image_meta(image, width, height),
                    }
                try:
                    state = await self.computer.state()
                    run.last_url = str(state.get("url") or run.last_url)
                except ComputerError:
                    pass

            run.status = STATUS_ERROR
            run.message = (
                f"stopped after {self.settings.computer_max_steps} steps without finishing"
            )
        except asyncio.CancelledError:
            run.status = STATUS_ERROR
            run.message = "Stopped"
        except Exception as exc:  # a background task must never die silently
            run.status = STATUS_ERROR
            run.message = f"computer control failed: {exc}"
        finally:
            run.finished_at = time.time()
            self._persist(run)

    async def _next_command(
        self,
        run: ComputerRun,
        bounds: Optional[Bounds],
        image: Optional[str],
        first_turn: bool,
    ):
        """Ask the model for one command, correcting its format at most twice.

        Returns ``(command, error, raw)``.  A ``None`` command means the run has
        already been given a terminal status and the caller must stop.
        """
        for attempt in range(self.settings.computer_max_json_retries + 2):
            # One trace entry per *request*, not per command.  A retry is a
            # second real call to the model with a different message list, and
            # the question the inspector has to answer is "what did the API
            # receive, and what came back" -- so a retry that overwrote the
            # first attempt's raw reply would erase the very reply that caused
            # the retry.  Overwriting made a run whose model ignored its
            # instructions look like a single clean failure.
            turn = ComputerTurnTrace(
                turn=len(run.trace) + 1,
                step=run.step + 1,
                timestamp=time.time(),
                task=run.task,
                first_turn=first_turn,
                attempt=attempt,
                allowed_types=list(
                    ("navigate", "search") if first_turn else ALLOWED_TYPES
                ),
                screenshot_types=list(SCREENSHOT_ACTIONS),
                json_only=True,
            )
            run.trace.append(turn)
            self._trim_trace(run)
            messages = self._history(
                run,
                bounds.width if bounds else 0,
                bounds.height if bounds else 0,
            )
            # Taken from the system message specifically, not from position
            # zero.  Indexing the list would label the user's text as "the
            # prompt" on any request where the system message went missing,
            # which is exactly the fault the inspector exists to surface.
            system = next((m for m in messages if m.role == "system"), None)
            turn.prompt = system.content if system else ""
            turn.prompt_attached = system is not None
            if image:
                # The correction rides in the *same* user turn as the image
                # rather than a second consecutive user message, which several
                # OpenAI-compatible endpoints reject outright.
                text = (
                    "Here is the latest screenshot of the real browser. "
                    "Its full size is the coordinate space for any click."
                )
            else:
                text = (
                    "You have not seen a screenshot yet. Your first command "
                    "must be navigate or search."
                )
            if attempt:
                # The rejection is the most recent thing the model has read, and
                # the image is still attached so it does not lose its bearings
                # while fixing a formatting mistake.
                text += f"\n\nThat command was rejected: {error}\n" + FORMAT_CORRECTION
            # Folded into the previous user turn rather than appended as a new
            # one.  The history already ends with the state of the last action,
            # and two user messages in a row is a hard error on some
            # OpenAI-compatible endpoints -- notably the Anthropic models
            # OpenRouter can route to, which require strictly alternating turns.
            if messages and messages[-1].role == "user" and not messages[-1].images:
                messages[-1] = LLMMessage(
                    role="user",
                    content=f"{messages[-1].content}\n\n{text}",
                    images=[image] if image else [],
                )
            else:
                messages.append(
                    LLMMessage(role="user", content=text, images=[image] if image else [])
                )

            # Recorded from the messages actually about to be sent, before the
            # call, so the inspector shows the request even if the call fails.
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
            turn.screenshot_attached = any(m.images for m in messages)
            turn.image = image or ""
            turn.image_meta = (
                _image_meta(image, bounds.width, bounds.height)
                if image and bounds
                else {}
            )

            try:
                raw, provider_name, model, wire = await self._ask(messages)
            except ProviderUnavailable as exc:
                turn.provider = ""
                turn.raw = ""
                turn.reply_timestamp = time.time()
                turn.error = str(exc)
                run.status = STATUS_ERROR
                run.message = str(exc)
                return None, str(exc), ""
            except Exception as exc:
                turn.provider = ""
                turn.raw = ""
                turn.reply_timestamp = time.time()
                turn.error = f"the computer-control model could not be reached: {exc}"
                run.status = STATUS_ERROR
                run.message = f"the computer-control model could not be reached: {exc}"
                return None, "", raw

            turn.provider = provider_name
            turn.model = model
            turn.wire = wire
            turn.reply_timestamp = time.time()
            # Verbatim.  Whatever came back is what gets shown, including prose,
            # a fenced code block, or nothing recognisable at all.
            turn.raw = raw

            command, error = parse_command(raw, bounds=bounds, first_turn=first_turn)
            turn.parse_ok = command is not None
            turn.parse_error = error
            if command is not None:
                turn.command = command.to_json()
                return command, "", raw

            self._record(run, {"type": "invalid"}, raw, "refused", error=error)
            if attempt >= self.settings.computer_max_json_retries:
                run.status = STATUS_ERROR
                run.message = f"the model never returned a usable command: {error}"
                return None, error, raw

        run.status = STATUS_ERROR
        run.message = "the model never returned a usable command"
        return None, "", ""

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

        Returns True when the run has reached a terminal state.
        """
        if command.type == "done":
            run.status = STATUS_DONE
            run.message = command.message or "Task complete."
            self._record(run, command.to_json(), "", run.message)
            return True
        if command.type == "error":
            run.status = STATUS_ERROR
            run.message = command.message or "The model reported it could not continue."
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
            self._record(run, command.to_json(), "", "failed", error=str(exc))
            run.message = str(exc)
            # The refusal is reported here and the caller in `_loop` turns the
            # recorded event into the turn's execution block, so it cannot be
            # lost just because it failed.
            return False


        self._record(run, command.to_json(), "", "ok", trace=trace)
        return False

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
