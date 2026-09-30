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
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..providers.base import LLMMessage, TextDelta
from ..providers.router import ProviderUnavailable, Router
from .commands import Bounds, Command, parse_command
from .controller import ComputerError, RemoteComputer
from .prompt import FORMAT_CORRECTION, build_prompt

# The only states a run can be in, and the only strings the frontend renders.
STATUS_IDLE = "idle"
STATUS_OBSERVING = "observing"
STATUS_CONTROLLING = "controlling"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass
class ComputerEvent:
    """One record of one turn, written to the database.

    Screenshot bytes are deliberately absent.  A dozen base64 JPEGs is several
    megabytes per run; the dimensions and hash are enough to prove afterwards
    which image a coordinate was read from.
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
            "running": self.status in (STATUS_OBSERVING, STATUS_CONTROLLING),
            "done": self.status in (STATUS_DONE, STATUS_ERROR),
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

    async def _ask(self, messages: List[LLMMessage]) -> str:
        """One model call, returning the reply as text.

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
        return "".join(parts)

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

                run.step += 1
                terminal = await self._perform(run, command)
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
        # Retried per turn, not per run: a model that is well behaved on turn
        # four should not still be paying for turn one.
        for attempt in range(self.settings.computer_max_json_retries + 2):
            messages = self._history(
                run,
                bounds.width if bounds else 0,
                bounds.height if bounds else 0,
            )
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

            try:
                raw = await self._ask(messages)
            except ProviderUnavailable as exc:
                run.status = STATUS_ERROR
                run.message = str(exc)
                return None, str(exc), ""
            except Exception as exc:
                run.status = STATUS_ERROR
                run.message = f"the computer-control model could not be reached: {exc}"
                return None, "", raw

            command, error = parse_command(raw, bounds=bounds, first_turn=first_turn)
            if command is not None:
                return command, "", raw

            self._record(run, {"type": "invalid"}, raw, "refused", error=error)
            if attempt >= self.settings.computer_max_json_retries:
                run.status = STATUS_ERROR
                run.message = f"the model never returned a usable command: {error}"
                return None, error, raw

        run.status = STATUS_ERROR
        run.message = "the model never returned a usable command"
        return None, "", ""

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
    digest = hashlib.sha256(image.encode("ascii", "ignore")).hexdigest()[:16]
    return {
        "width": width,
        "height": height,
        "sha256_16": digest,
        "bytes_b64": len(image),
    }
