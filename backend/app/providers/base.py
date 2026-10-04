from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Dict, List, Optional, Union

from .errors import ModelCallGateTimeout, ProviderHTTPError


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMMessage:
    role: str
    content: str = ""
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: str = ""
    name: str = ""
    images: List[str] = field(default_factory=list)


@dataclass
class TextDelta:
    content: str


@dataclass
class ToolCallEvent:
    call: ToolCall


@dataclass
class Done:
    stop_reason: str = "stop"


LLMEvent = Union[TextDelta, ToolCallEvent, Done]
ToolSchema = Dict[str, object]


@dataclass
class Provider:
    name: str = "provider"

    #: What the last outgoing request actually contained, summarised.
    #:
    #: Set by each provider from the real body it is about to send, not
    #: reconstructed afterwards from application state.  The computer-control
    #: loop needs to be able to prove the screenshot was on the wire, and an
    #: image sitting in a message object is not evidence of that -- the
    #: serialisation is what decides, and only the serialiser knows.
    last_wire: Optional[Dict[str, object]] = None

    #: Token counts the API reported for the last request, as ``{prompt,
    #: completion}``.  ``{}`` when the provider sends none.
    #:
    #: This is the only honest measure of what a request cost.  Counting
    #: characters locally is an estimate of a number the vendor already told us,
    #: and an estimate cannot answer the question a control loop actually needs
    #: answered: whether the request that carried a screenshot was worth more
    #: than the one that did not.
    last_usage: Optional[Dict[str, int]] = None

    #: Why the last request stopped, as the provider reported it -- ``stop``,
    #: ``length``, ``tool_calls``, ``content_filter``.
    #:
    #: Recorded here, next to `last_wire`, because both are facts about the last
    #: call that only the provider can know.  They are also what turns "the model
    #: returned an empty response" into a diagnosis: a reply with no content and
    #: no tool call means something completely different when the provider said
    #: `length` (the completion budget ran out first) than when it said
    #: `tool_calls` (a call was sent that the parser could not read).
    last_stop_reason: Optional[str] = None

    #: Seconds the last request spent queued for the model-call gate before it was
    #: sent, including any wait to re-enter after a rate limit.
    #:
    #: Recorded here for the same reason as `last_wire`: a request that has not
    #: been sent has no wire summary, no usage and no stop reason, and those three
    #: absences are indistinguishable from a slow provider.  This is the one fact
    #: that tells them apart -- a non-zero value means the delay was ours, spent
    #: in a queue, and says so before the request is over.  ``0.0`` means the gate
    #: was free and the provider had the whole wait.
    last_gate_wait: float = 0.0

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        raise NotImplementedError


MODEL_CALL_GAP_SECONDS = 10.0

#: Consecutive 429s one logical request absorbs before it gives up.  One rate
#: limit still recovers exactly as it always did -- same request, retried at the
#: provider's own reset time, still one logical call.  The cap only bounds the
#: pathological case: a provider (or a free tier) that answers 429 forever, which
#: used to park the gate indefinitely and freeze every other model call in the
#: process with no error anywhere.
MODEL_MAX_RATE_LIMIT_RETRIES = 3

#: How long one 429 may hold *other* callers back.
#:
#: Separate from the budget below because they answer different questions.  This
#: one is "may a rate limit freeze the rest of the app", and the answer is a
#: small number: a parked request hands the gate back, and this is how long the
#: reset time it published still holds everyone else.  Long enough to stop a
#: burst of doomed calls, short enough that an advertised "come back in 18
#: minutes" does not stop the rest of the app from working for 18 minutes.
MODEL_RATE_LIMIT_FLOOR_MAX_SECONDS = 120.0

#: Total time one logical request will spend parked on rate limits before giving
#: up.  Bounds the same case from the other side, and it is deliberately
#: generous: an advertised reset time is honoured in full, once, because
#: OpenRouter sends resets like "try again in 18m10.3s" and a request that gave
#: up instead of waiting would throw away a recovery that was guaranteed.  What
#: this bounds is repetition -- several long resets in a row -- and hours.
MODEL_MAX_RATE_LIMIT_STALL_SECONDS = 1800.0

#: Longest a request will queue for the gate before it is told the queue is the
#: problem.  Sized above the worst legitimate wait rather than picked: a published
#: rate-limit floor (120s) plus one full provider call at the transport's own
#: read timeout (180s) plus the inter-call gap (10s) is 310s, so a healthy request
#: that arrives behind a rate limit is never mistaken for a stuck one.  And it is
#: still a bound, which is the entire point: the queue is now able to say no.
MODEL_CALL_GATE_MAX_WAIT_SECONDS = 420.0

#: How often a waiter re-checks whether the gate is free.  A gate wait is
#: measured in seconds, not microseconds, so polling costs nothing measurable --
#: and polling is used in preference to a cancellable acquire, because
#: cancelling an `asyncio.Lock.acquire()` that has just been granted can leave
#: the lock held by nobody, which would wedge every later call.
MODEL_CALL_GATE_POLL_SECONDS = 0.05


class _ModelCallGate:
    """Serialize model calls with a hard 10s gap between normal calls.

    One gate for every role in the app (commander, worker, browser, computer),
    because the limit that matters is the provider's, not this process's.

    A rate-limited request *parks* here rather than holding the lock: it publishes
    the provider's reset time as a floor that nobody may start before, hands the
    gate back so nothing else is frozen, and re-acquires it when the floor has
    passed.  The original guarantee survives in the form that matters -- a newer
    message does not get to run ahead of a rate-limited one and consume the quota
    the rate-limited one is waiting for -- while the failure that guarantee was
    bought with is gone: one stalled request no longer blocks every other call in
    the process for as long as the provider cares to say.  The floor itself is
    capped, so a provider advertising a reset hours out cannot reinstate that
    block through the published-time side door.
    """

    def __init__(self) -> None:
        import asyncio
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        #: Earliest time any call may start, published by a rate-limited request.
        self._stalled_until = 0.0
        #: How many callers are inside `acquire`, holding the gate or not.  A
        #: count rather than a flag, because "is somebody queued" is a question
        #: about people, and a flag cannot survive one of them giving up.
        self._waiters = 0
        #: Epoch seconds at which the current queue formed, 0 when nobody waits.
        self._waiting_since = 0.0
        #: What holds the gate, for the error message of whoever cannot get it.
        self._holder = ""

    async def acquire(
        self,
        *,
        respect_gap: bool = True,
        max_wait: Optional[float] = MODEL_CALL_GATE_MAX_WAIT_SECONDS,
    ) -> float:
        """Take the gate, waiting for the gap and any published rate limit.

        Returns how long the caller waited, which is the only honest measure of
        what the queue cost it.  Raises `ModelCallGateTimeout` rather than
        waiting without end.

        Polls rather than waiting on the lock directly, so the wait can be bounded
        without ever cancelling an acquire that the lock may just have granted --
        the failure mode where a cancelled waiter leaves the lock held by nobody
        and every later call queues behind a gate nobody can open.  The cost is
        that the queue is no longer strictly first-come, which in practice costs
        nothing here: every released gate is followed by a ten-second gap in which
        the lock is free and the next caller takes it outright, so callers only
        ever compete against a call that is already in flight.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        started = loop.time()
        self._waiters += 1
        if not self._waiting_since:
            self._waiting_since = time.time()
        try:
            while self._lock.locked():
                if max_wait is not None and loop.time() - started >= max_wait:
                    raise ModelCallGateTimeout(waited=loop.time() - started, held_by=self._holder)
                await asyncio.sleep(MODEL_CALL_GATE_POLL_SECONDS)
            await self._lock.acquire()
            try:
                # Checked again here, not only in the polling loop above: the lock
                # can be taken by another coroutine in the instant between the loop
                # seeing it free and this acquire winning it, and that call runs for
                # as long as a provider call runs.  A request that has already
                # waited longer than the budget is told so as soon as it holds the
                # gate rather than served after the fact -- and it hands the gate
                # straight back, so the next waiter is not delayed by this one
                # discovering it was late.
                elapsed = loop.time() - started
                if max_wait is not None and elapsed >= max_wait:
                    raise ModelCallGateTimeout(waited=elapsed, held_by=self._holder)
                # A parked rate-limited request owns the floor until its reset
                # time, so a re-acquiring one ignores the gap other traffic has
                # been pushing forward but still never starts early.
                floor = self._stalled_until
                if respect_gap:
                    floor = max(floor, self._next_allowed)
                wait = floor - loop.time()
                if wait > 0:
                    budget = None if max_wait is None else max(0.0, max_wait - (loop.time() - started))
                    if budget is not None and wait > budget:
                        raise ModelCallGateTimeout(waited=loop.time() - started, held_by=self._holder)
                    await asyncio.sleep(wait)
            except BaseException:
                self._lock.release()
                raise
            return loop.time() - started
        finally:
            self._waiters = max(0, self._waiters - 1)
            if not self._waiters:
                self._waiting_since = 0.0

    def finish_success(self) -> None:
        import asyncio
        self._next_allowed = asyncio.get_running_loop().time() + MODEL_CALL_GAP_SECONDS

    def stall_for(self, until: float) -> None:
        """Publish a rate limit: no call may start before ``until``.

        Loop time, so it is comparable with `_next_allowed`.  The floor is capped
        so one provider advertising a reset hours away cannot re-create the frozen
        gate through the back door -- the parked request still waits its own full
        reset time and retries then, but it does not make everyone else wait for
        it.
        """
        import asyncio
        moment = asyncio.get_running_loop().time()
        floor = max(moment, min(until, moment + MODEL_RATE_LIMIT_FLOOR_MAX_SECONDS))
        self._stalled_until = max(self._stalled_until, floor)
        self._next_allowed = max(self._next_allowed, self._stalled_until)

    def clear_stall(self) -> None:
        """The provider is answering again, so the published floor is void."""
        self._stalled_until = 0.0

    def set_holder(self, label: str) -> None:
        self._holder = label

    def release(self) -> None:
        self._holder = ""
        self._lock.release()

    def snapshot(self) -> Dict[str, object]:
        """What the queue looks like right now, for the panel.

        Read live rather than remembered: the one fact that changes while a
        request waits is how long it has been waiting, and a value written at the
        start of the wait cannot show it.  ``waiting_since`` is 0 while nobody is
        queued, so a busy gate with an empty queue reads as busy rather than as a
        wait that never started.
        """
        return {
            "busy": bool(self._lock.locked() or self._waiters),
            "waiting_since": self._waiting_since if self._waiters else 0.0,
            "held_by": self._holder,
        }


_MODEL_CALL_GATE = _ModelCallGate()


def model_call_gate_snapshot() -> Dict[str, object]:
    """Live state of the shared model-call gate."""
    return _MODEL_CALL_GATE.snapshot()


def model_call_gate_label(provider: "Provider", model: str, caller: str = "") -> str:
    """The name a call publishes while it holds the gate.

    ``provider/model``, plus the caller when there is one.  Provider and model
    alone are not enough: two roles can resolve to the same pair, and a label
    that cannot tell them apart cannot answer the only question a waiter has,
    which is whether the call in front of it is its own.
    """
    name = str(getattr(provider, "name", "") or "provider")
    who = f"{name}/{model}" if model else name
    return f"{who} #{caller}" if caller else who


async def stream_model(
    provider: "Provider",
    messages: List[LLMMessage],
    tools: List[ToolSchema],
    model: str,
    *,
    on_rate_limit: Optional[Callable[[Exception], object]] = None,
    caller: str = "",
) -> AsyncIterator[LLMEvent]:
    """Run one logical model request with normal pacing and bounded 429 recovery.

    Successful/completed calls are separated by 10 seconds. A 429 does NOT use
    that 10-second gap: the same request stays stalled and is retried exactly
    when the provider says the limit resets. There is no control-loop timeout
    around this wait.

    Three bounds, each added because the unbounded version could not report what
    it was doing and so could only appear to hang:

    1. The queue is bounded.  A request that cannot get the gate within
       `MODEL_CALL_GATE_MAX_WAIT_SECONDS` is told the queue was the problem,
       rather than sitting on an empty trace entry that looks like a slow model.
    2. Rate-limit recovery is bounded.  The same request, at the provider's own
       reset time, up to `MODEL_MAX_RATE_LIMIT_RETRIES` times and
       `MODEL_MAX_RATE_LIMIT_STALL_SECONDS` in total; past that the provider's
       own 429 is raised with the give-up recorded, because a provider that
       answers 429 forever is a fact about the provider and not something to
       retry silently.
    3. A parked request does not hold the gate.  It publishes the reset time as a
       floor for everyone, capped, and hands the gate back, so a rate-limited
       agent call cannot freeze computer control -- while still not being able to
       be overtaken, because nobody else starts before that floor either.

    Every wait is recorded on the provider as `last_gate_wait`, in seconds,
    because "how long was this queued" is the difference between a slow provider
    and a busy queue and neither the panel nor the runner can tell them apart
    without it.

    `caller` names who is asking, and is folded into the label this call publishes
    while it holds the gate.  Without it a computer-control request and an agent
    request that happened to resolve to the same provider and model would be
    indistinguishable, and a panel asked "are you the one running or the one
    waiting?" could only guess.
    """
    from inspect import isawaitable
    import asyncio

    label = model_call_gate_label(provider, model, caller)

    def note_wait() -> None:
        try:
            provider.last_gate_wait = round(_gate_waited, 3)
        except Exception:  # pragma: no cover - defensive
            pass

    _gate_waited = 0.0
    try:
        _gate_waited += await _MODEL_CALL_GATE.acquire()
    except ModelCallGateTimeout as exc:
        _gate_waited += exc.waited
        note_wait()
        raise ModelCallGateTimeout(
            provider=getattr(provider, "name", ""),
            model=model,
            waited=_gate_waited,
            held_by=exc.held_by,
        )
    note_wait()
    _MODEL_CALL_GATE.set_holder(label)
    locked = True
    rate_limit_retries = 0
    rate_limit_waited = 0.0
    try:
        while True:
            try:
                async for event in provider.stream(messages, tools, model):
                    if isinstance(event, Done):
                        # Recorded on the provider, next to `last_wire` and
                        # `last_usage`, because it is the same kind of fact: only
                        # the provider knows why the call stopped.  The runner
                        # reads it after draining the stream, and without it an
                        # empty reply is undiagnosable -- "the model returned an
                        # empty response" covers a truncated reasoning trace, a
                        # filtered answer and a call the parser dropped, and
                        # those three need three different fixes.
                        try:
                            provider.last_stop_reason = event.stop_reason
                        except Exception:  # pragma: no cover - defensive
                            pass
                    yield event
                _MODEL_CALL_GATE.clear_stall()
                _MODEL_CALL_GATE.finish_success()
                _MODEL_CALL_GATE.release()
                locked = False
                return
            except ProviderHTTPError as exc:
                if exc.status != 429:
                    _MODEL_CALL_GATE.clear_stall()
                    _MODEL_CALL_GATE.finish_success()
                    _MODEL_CALL_GATE.release()
                    locked = False
                    raise
                rate_limit_retries += 1
                if on_rate_limit is not None:
                    note = on_rate_limit(exc)
                    if isawaitable(note):
                        await note
                wait = float(exc.retry_after if exc.retry_after is not None else MODEL_CALL_GAP_SECONDS)
                rate_limit_waited += max(0.0, wait)
                if (
                    rate_limit_retries > MODEL_MAX_RATE_LIMIT_RETRIES
                    or rate_limit_waited > MODEL_MAX_RATE_LIMIT_STALL_SECONDS
                ):
                    # Bounded now.  Reported as the provider's own 429 with the
                    # give-up attached, so the runner records a real status and a
                    # real body instead of a synthesized failure, and the panel
                    # can still show what the provider said every time it said it.
                    _MODEL_CALL_GATE.clear_stall()
                    _MODEL_CALL_GATE.finish_success()
                    _MODEL_CALL_GATE.release()
                    locked = False
                    raise _rate_limit_gave_up(exc, rate_limit_retries, rate_limit_waited)
                # Park: publish the reset time so no call starts before it, then
                # hand the gate back so the rest of the app keeps working.  The
                # stall floor is what prevents an overtake, not the lock.
                _MODEL_CALL_GATE.stall_for(asyncio.get_running_loop().time() + max(0.0, wait))
                _MODEL_CALL_GATE.release()
                locked = False
                try:
                    if wait > 0:
                        await asyncio.sleep(wait)
                    _gate_waited += await _MODEL_CALL_GATE.acquire(respect_gap=False)
                    note_wait()
                    _MODEL_CALL_GATE.set_holder(label)
                    locked = True
                except ModelCallGateTimeout as exc:
                    _MODEL_CALL_GATE.clear_stall()
                    raise ModelCallGateTimeout(
                        provider=getattr(provider, "name", ""),
                        model=model,
                        waited=_gate_waited + exc.waited,
                        held_by=exc.held_by,
                    )
                except BaseException:
                    _MODEL_CALL_GATE.clear_stall()
                    raise
                continue
            except asyncio.CancelledError:
                _MODEL_CALL_GATE.release()
                locked = False
                raise
            except Exception:
                _MODEL_CALL_GATE.finish_success()
                _MODEL_CALL_GATE.release()
                locked = False
                raise
    finally:
        if locked:
            _MODEL_CALL_GATE.release()


def _rate_limit_gave_up(
    exc: ProviderHTTPError, retries: int, waited: float
) -> ProviderHTTPError:
    """The provider's own 429, with the fact that we stopped asking attached."""
    return ProviderHTTPError(
        provider=exc.provider,
        model=exc.model,
        status=exc.status,
        reason=exc.reason,
        body=(
            f"{exc.body}\n"
            f"(stopped retrying after {retries} rate-limit retries over "
            f"{waited:.0f}s: the provider's reset time never arrived, so this "
            f"request was abandoned rather than retried forever)"
        ).strip(),
        retry_after=exc.retry_after,
    )


def tool_schema_openai(tool: ToolSchema) -> ToolSchema:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        },
    }


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text) - limit} chars omitted]"


def image_mime(data_url_b64: str) -> str:
    if data_url_b64.startswith("/9j/"):
        return "image/jpeg"
    if data_url_b64.startswith("iVBOR"):
        return "image/png"
    if data_url_b64.startswith("R0lGOD"):
        return "image/gif"
    if data_url_b64.startswith("UklGR"):
        return "image/webp"
    return "image/png"


def summarize_wire(
    body: Dict[str, object],
    messages: List[LLMMessage],
    path: str = "",
) -> Dict[str, object]:
    """Describe a request body that is about to be sent, for the inspector.

    Reads the serialised body rather than the message objects it came from.
    That distinction is the whole point: the question is not "did an image exist
    in application state" but "is an image part present in the bytes going to
    the API", and only the serialised form can answer the second one.

    ``serialized_ok`` is the answer to the only question a count cannot answer
    on its own: *was this body actually serialisable, and did every message the
    caller built survive into it*.  It is decided here, by the code doing the
    serialising, because that is the only place the answer exists -- and the
    frontend is told the result instead of being left to compare two numbers and
    guess.  ``serialization_error`` says why, when it is false, so a red badge is
    a diagnosis rather than an accusation.

    Never includes the base64 itself, the API key, or any header value.
    """
    raw_messages = body.get("messages")
    messages_is_list = isinstance(raw_messages, list)
    wire_messages: list = raw_messages if messages_is_list else []

    serialized_ok, serialization_error = _serialisation_verdict(
        body, messages_is_list, len(messages)
    )

    roles: List[str] = []
    image_count = 0
    image_mimes: List[str] = []
    image_payload_type = ""
    text_count = 0
    part_types: List[str] = []
    first_image_at: Optional[int] = None

    for index, message in enumerate(wire_messages):
        if not isinstance(message, dict):
            continue
        roles.append(str(message.get("role") or "?"))
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                text_count += 1
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "")
            part_types.append(kind)
            if kind != "image_url":
                if kind == "text":
                    text_count += 1
                continue
            image_count += 1
            image_payload_type = kind
            if first_image_at is None:
                first_image_at = index
            url = part.get("image_url")
            mime = ""
            if isinstance(url, dict):
                raw = str(url.get("url") or "")
                if raw.startswith("data:"):
                    head, _, _payload = raw.partition(",")
                    mime = head[len("data:"):].split(";")[0]
            image_mimes.append(mime)

    return {
        "path": path,
        "model": body.get("model"),
        "messages_count": len(wire_messages),
        "roles": roles,
        "text_parts": text_count,
        "image_count": image_count,
        "image_present": image_count > 0,
        "image_mime": image_mimes[0] if image_mimes else "",
        "image_payload_type": image_payload_type,
        "content_part_types": part_types,
        "first_image_message_index": first_image_at,
        "stream": bool(body.get("stream")),
        "response_format": body.get("response_format"),
        "tools_count": len(body.get("tools") or []),
        # For cross-checking: the messages the caller passed in, so a mismatch
        # between intent and wire is visible rather than silent.
        "source_message_count": len(messages),
        # The verdict itself, so the inspector renders what the serialiser knows
        # rather than re-deriving it from the two counts above.  A turn with no
        # wire summary at all has no `serialized_ok` key, which the frontend
        # reads as "not reported" -- a third state, and the one that used to be
        # rendered as a failure.
        "serialized_ok": serialized_ok,
        "serialization_error": serialization_error,
    }


def _serialisation_verdict(
    body: Dict[str, object],
    messages_is_list: bool,
    source_count: int,
) -> tuple:
    """``(serialized_ok, why_not)`` for a body that is about to be sent.

    Two questions, and they fail differently, so both are asked here rather than
    in the panel:

    1. *Can this body be encoded at all?*  Checked by encoding it, which is the
       same thing the HTTP client is about to do.  A body holding something
       JSON cannot represent would otherwise fail at the socket with a
       ``TypeError`` that names a field and not the request.
    2. *Did every message the caller built reach the wire?*  Counted, because a
       serialiser that silently drops a message it does not recognise produces a
       shorter conversation and a run that cannot see its own history, with
       nothing in the trace to say so.

    A count of more than the caller built is the same fault seen from the other
    side -- a message invented in serialisation -- and is reported as the
    mismatch it is rather than passing as "at least everything got there".
    """
    if not messages_is_list:
        return False, "the request body has no serialised message list"
    try:
        json.dumps(body)
    except (TypeError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    wire_count = len(body.get("messages") or [])
    if wire_count != source_count:
        return False, (
            f"the request carried {wire_count} of the {source_count} messages "
            f"the runner built"
        )
    return True, ""
