from __future__ import annotations

import json
import sys
from typing import AsyncIterator, List

import httpx

from .base import (
    Done,
    LLMEvent,
    LLMMessage,
    Provider,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSchema,
    image_mime,
    summarize_wire,
    tool_schema_openai,
    truncate,
)
from .errors import ProviderHTTPError


async def _error_body(resp: "httpx.Response") -> str:
    """Whatever the provider said, as text.

    Never raises: a body that fails to decode is itself information about a
    broken response, and losing it would leave the trace claiming an error
    message that the provider never gave.
    """
    try:
        return (await resp.aread()).decode("utf-8", "replace")
    except Exception:
        # ``except ... as name`` unbinds the name at the end of the block, so
        # the reason is read out of ``exc_info`` instead of captured.  Losing it
        # would leave the trace claiming an error message the provider never
        # gave, which is the failure this whole path exists to prevent.
        return f"<unreadable response body: {sys.exc_info()[1]}>"


def _reason(resp: "httpx.Response") -> str:
    """The status phrase, e.g. "Too Many Requests"."""
    phrase = getattr(resp, "reason_phrase", "") or ""
    return str(phrase).strip()


def _content_text(content) -> str:
    """`message.content` as plain text, whichever shape it arrived in.

    The spec allows a string or a list of content parts, and OpenAI-compatible
    endpoints differ on which they send.  A list rendered with ``str()`` would
    reach the caller as a Python repr with quotes and braces around it, which
    corrupts any structured text in the content -- the computer loop's
    ``{"history": ...}`` object, for one, would no longer be extractable.  So the
    text parts are joined and the rest is dropped, rather than stringified.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") in ("text", "output_text", ""):
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def _retry_after(resp: "httpx.Response"):
    """``Retry-After`` in seconds, when the provider sent a usable one.

    The header is defined in seconds but is routinely sent as an HTTP-date, and
    a date in the past means "retry now".  Anything unparseable is treated as
    absent so the caller falls back to its own bounded backoff rather than
    trusting a value it could not read.
    """
    raw = ""
    try:
        raw = resp.headers.get("retry-after", "")
    except Exception:  # pragma: no cover - defensive
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone

            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return None
    return max(0.0, seconds)


def _arguments_json(value) -> str:
    """A tool call's arguments as the JSON *string* the parser expects.

    The spec says a string, and most endpoints send one.  Not all do: an
    OpenAI-compatible endpoint may send ``arguments`` already decoded, as an
    object.  Stringifying that with ``str()`` is what turns a perfectly valid
    call into ``{'x': 344, 'y': 107}`` -- single quotes, which is not JSON -- and
    the runner then refuses a tool call that was correct, reporting "the
    arguments of click() were not valid JSON" for a call that carried valid
    arguments.  So anything that is not a string is re-encoded as JSON.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return "{}"


def _message_tool_calls(message) -> List[ToolCall]:
    """The native tool calls in one complete ``message``, whatever shape they came in.

    ``content: null`` with a call on it is the *normal* reply of a tool-calling
    model and not a defect, so nothing here requires any text to be present.

    Three spellings are accepted, because OpenAI-compatible endpoints use all
    three and a loop that reads only one of them reports "the model returned an
    empty response" for a call that was delivered:

    - ``tool_calls: [{id, type, function: {name, arguments}}]`` -- the current one.
    - ``function_call: {name, arguments}`` -- the single-call spelling from
      before ``tool_calls`` existed, still answered by some endpoints.
    - the fields hoisted onto the entry itself (``{name, arguments}``) with no
      ``function`` wrapper at all.

    Entries that are not objects, and calls with no name, are dropped rather than
    turned into an empty tool call the runner would then have to diagnose.
    """
    if not isinstance(message, dict):
        return []
    raw = message.get("tool_calls")
    entries = list(raw) if isinstance(raw, list) else []
    if not entries:
        legacy = message.get("function_call")
        entries = [legacy] if isinstance(legacy, dict) else []
    calls: List[ToolCall] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        function = function if isinstance(function, dict) else {}
        name = str(function.get("name") or entry.get("name") or "")
        if not name:
            continue
        arguments = function.get("arguments", entry.get("arguments"))
        calls.append(
            ToolCall(
                id=str(entry.get("id") or f"call_{index}"),
                name=name,
                arguments=_arguments_json(arguments),
            )
        )
    return calls


def _completion_error(payload, provider: str, model: str, streamed: bool = False):
    """A 200 that is not a completion, raised as the error it actually is.

    OpenRouter answers a request it cannot satisfy -- a routed model with no tool
    support, a router with no free endpoint available -- with ``{"error": {...}}``
    and **no** ``choices``, sometimes under HTTP 200.  Read as a completion that
    is an empty model reply, which is not what happened: the model was never
    asked, and the trace then reports "(the model returned an empty response)"
    for a request the provider refused by name.  So the body is checked before it
    is parsed as a reply, and a refusal is raised with the provider's own words
    attached.

    Status 200 is reported honestly rather than dressed up as a 4xx: the provider
    was reached and did answer, and it is not retryable -- resending an identical
    request gets the identical refusal.
    """
    body = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, (str, dict, list)) and error:
            body = error if isinstance(error, str) else json.dumps(error)
    elif isinstance(payload, (str, bytes)):
        body = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
    elif payload is not None:
        body = str(payload)
    detail = body.strip()
    if not detail:
        if streamed:
            return ProviderHTTPError(
                provider=provider,
                model=model,
                status=200,
                reason="OK (stream ended with no completion)",
                body="the stream carried no chat completion chunk",
            )
        return ProviderHTTPError(
            provider=provider,
            model=model,
            status=200,
            reason="OK (no completion in the response)",
            body=json.dumps(payload) if payload is not None else "",
        )
    return ProviderHTTPError(
        provider=provider,
        model=model,
        status=200,
        reason="OK (error body, no completion)",
        body=detail,
    )


class OpenAICompatProvider(Provider):
    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str,
        timeout: float = 180.0,
        max_completion_tokens: int = 0,
        reasoning_effort: str = "",
    ):
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Both are optional body fields rather than hardcoded values, because
        # they are not interchangeable across the OpenAI-compatible endpoints:
        # some reject an unknown body key outright, and one that does is a
        # provider that cannot be used at all.  Left unset they are simply not
        # sent, which is what the endpoints that do not know them expect.
        self.max_completion_tokens = int(max_completion_tokens or 0)
        self.reasoning_effort = str(reasoning_effort or "").strip()
        #: Which body key carries the ceiling.  `max_completion_tokens` is the
        #: OpenAI spelling and the one Groq and OpenRouter expect, but Mistral
        #: documents the older `max_tokens` and treats the newer name as unknown
        #: on some deployments.  So the field name is a property of the endpoint
        #: rather than something written into the body at random.
        self.token_limit_field = "max_completion_tokens"

    def _wire_messages(self, messages: List[LLMMessage]) -> list:
        out = []
        for m in messages:
            if m.role == "system":
                out.append({"role": "system", "content": m.content})
            elif m.role == "user":
                # A user turn may carry the screenshot.  The OpenAI wire format
                # only accepts an image inside a user turn as a content *part*,
                # so once there is an image this becomes a list; a plain text
                # turn stays a plain string, which is what providers without
                # vision expect and costs nothing.
                if m.images:
                    parts: list = []
                    if m.content:
                        parts.append({"type": "text", "text": m.content})
                    for img in m.images:
                        if img:
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{image_mime(img)};base64,{img}",
                                        "detail": "high",
                                    },
                                }
                            )
                    out.append({"role": "user", "content": parts})
                else:
                    out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                msg = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in m.tool_calls
                    ]
                out.append(msg)
            elif m.role == "tool":
                # Always a string, never a content-part list.  Groq validates
                # this field strictly and answers a list with
                # `messages[3].content must be a string`, which fails the whole
                # request rather than the message -- so a screenshot that had
                # been attached to its own tool result made every capture a
                # 400.  The image rides on the user turn instead, and the result
                # says in words what the frame is.
                content = m.content or ""
                if not isinstance(content, str):
                    # A caller that attached parts to a tool result gets text out
                    # rather than a rejected request, with the images dropped
                    # rather than silently duplicated onto another turn.
                    content = " ".join(
                        str(part.get("text", "")) for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ).strip() or "ok"
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": content})
        return out

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        url = f"{self.base_url}/chat/completions"
        # Groq computer-control calls use a complete response. The tool call
        # is tiny, and a complete response avoids an empty/partial SSE turn.
        use_stream = str(self.name).strip().lower() not in {"groq"}
        body = {
            "model": model,
            "messages": self._wire_messages(messages),
            "stream": use_stream,
            "temperature": 0.0,
        }
        if tools:
            # Converted here rather than by the caller, because the loop hands
            # every provider the same provider-neutral schema and each endpoint
            # wants a different envelope around it.  Building the envelope at the
            # call site would mean a second copy of it per provider, and a tool
            # list sent in the wrong shape is rejected as a 400 rather than
            # ignored -- so the mistake shows up as the feature not existing.
            body["tools"] = [tool_schema_openai(t) for t in tools]
            # OpenRouter/Groq computer models should not spend a turn replying
            # with prose or an empty delta when the contract requires one native
            # tool call.  Requiring a tool call removes an otherwise expensive
            # protocol-retry request.  Mistral is left on its default behavior
            # because its compatible deployments differ in tool-choice support.
            if str(self.name).strip().lower() in {"openrouter", "groq"}:
                body["tool_choice"] = "required"
                body["parallel_tool_calls"] = False
        if self.max_completion_tokens > 0:
            # A control loop emits one short tool call per turn.  A generous
            # ceiling here does not make the model verbose -- it makes a verbose
            # turn *affordable*, and on a per-token provider that is the
            # difference between a run that fits in a budget and one that does
            # not.
            body[self.token_limit_field] = self.max_completion_tokens
        if self.reasoning_effort:
            # Reasoning tokens are billed but never visible to the caller, so a
            # thinking model asked to emit one small JSON object can spend more
            # on deliberation than on the answer.  Forcing the cheapest mode is
            # what makes "tiny output" a property of the request rather than a
            # hope about the model's mood.
            body["reasoning_effort"] = self.reasoning_effort
        # Ask for the token counts to come back on the stream.  Without this the
        # final chunk is a `[DONE]` and nothing ever learns what a request cost,
        # which is the one number that decides whether the screenshot belongs in
        # the request at all.
        if use_stream:
            body["stream_options"] = {"include_usage": True}
        # Recorded from the body that is about to go out, before it is sent, so
        # the inspector shows what the API was actually given.  Headers and the
        # key are not part of it, and the base64 is not copied into the summary.
        self.last_wire = summarize_wire(body, messages, path="/chat/completions")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = httpx.Timeout(self.timeout, connect=15.0)
        acc: dict = {}
        order: list = []
        usage: Dict[str, int] = {}
        #: Why the provider said the turn ended, as it reported it.  Empty means
        #: it never said, which is itself reported as such rather than turned
        #: into a guess: the runner has to be able to tell "no reason given" from
        #: "ran out of completion budget" from "stopped after a tool call".
        stop_reason = ""
        self.last_usage = {}
        async with httpx.AsyncClient(timeout=timeout) as client:
            if not use_stream:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code >= 400:
                    detail = await _error_body(resp)
                    raise ProviderHTTPError(
                        provider=self.name,
                        model=model,
                        status=resp.status_code,
                        reason=_reason(resp),
                        body=detail,
                        retry_after=_retry_after(resp),
                    )
                try:
                    payload = resp.json()
                except Exception as exc:
                    raise RuntimeError(f"provider returned invalid JSON: {exc}")
                reported = payload.get("usage") if isinstance(payload, dict) else None
                if isinstance(reported, dict):
                    usage = {
                        "prompt_tokens": int(reported.get("prompt_tokens") or 0),
                        "completion_tokens": int(reported.get("completion_tokens") or 0),
                        "total_tokens": int(reported.get("total_tokens") or 0),
                    }
                choices = payload.get("choices") if isinstance(payload, dict) else None
                # Checked before anything is read out of the body.  A refusal
                # that arrives with no `choices` has no reply in it at all, and
                # parsing it as a reply is what turned an OpenRouter error into
                # "the model returned an empty response" -- a report about a
                # model that was never reached, from a request it refused by name.
                if isinstance(payload, dict) and (
                    payload.get("error") or not isinstance(choices, list) or not choices
                ):
                    raise _completion_error(payload, self.name, model)
                first = choices[0] if choices and isinstance(choices[0], dict) else {}
                message = first.get("message") if isinstance(first, dict) else {}
                if not isinstance(message, dict):
                    message = {}
                content = _content_text(message.get("content"))
                if content:
                    yield TextDelta(content)
                # A tool call is a complete answer on its own, and the note the
                # loop remembers it by is an *argument* of that call -- see
                # `app.computer.history`.  Text is yielded here exactly as it
                # arrived, for the trace; `content: null` -- and `content: ""` --
                # yield nothing, which is the normal case on this endpoint and
                # not a fault.  Neither part requires the other.
                for call in _message_tool_calls(message):
                    yield ToolCallEvent(call)
                self.last_usage = dict(usage)
                yield Done(stop_reason=str(first.get("finish_reason") or "stop"))
                return

            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    # Read the body before raising.  A streamed response has to
                    # be drained for its content to be available at all, and
                    # this is the only place the provider's own explanation of
                    # the refusal exists -- the status line alone says "429"
                    # and not whether to wait a second or an hour.
                    detail = await _error_body(resp)
                    raise ProviderHTTPError(
                        provider=self.name,
                        model=model,
                        status=resp.status_code,
                        reason=_reason(resp),
                        body=detail,
                        retry_after=_retry_after(resp),
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # A frame that is not an object carries no completion and no
                    # error; read past it rather than attribute a field to it.
                    if not isinstance(chunk, dict):
                        continue
                    # A refusal delivered inside the stream.  OpenRouter reports
                    # a request it cannot route as a frame carrying `error` and
                    # no choices; skipping it, as an earlier version did, leaves
                    # the caller with an empty reply and no idea that the
                    # provider had answered with a refusal instead of a
                    # completion.
                    if chunk.get("error"):
                        raise _completion_error(chunk, self.name, model, streamed=True)
                    # The usage frame carries no choices, so it has to be read
                    # before the `if not chunk.get("choices")` skip below --
                    # otherwise the one chunk that says what the request cost is
                    # the one chunk that gets discarded.
                    reported = chunk.get("usage")
                    if isinstance(reported, dict):
                        usage = {
                            "prompt_tokens": int(reported.get("prompt_tokens") or 0),
                            "completion_tokens": int(reported.get("completion_tokens") or 0),
                            "total_tokens": int(reported.get("total_tokens") or 0),
                        }
                    if not chunk.get("choices"):
                        continue
                    first = chunk["choices"][0] if isinstance(chunk["choices"][0], dict) else {}
                    delta = first.get("delta")
                    delta = delta if isinstance(delta, dict) else {}
                    # Why the turn ended, kept because it is the only thing that
                    # separates "the model had nothing to say" from "the model
                    # ran out of completion budget before it said anything" --
                    # and a stream that ends with `finish_reason: length`, no
                    # content and no tool call is indistinguishable from an
                    # empty model reply without it.
                    if first.get("finish_reason"):
                        stop_reason = str(first.get("finish_reason"))
                    # Accumulated as text whatever shape the delta used, and
                    # joined across chunks: a streamed reply can split the
                    # computer loop's history JSON anywhere, including inside a
                    # string, and the object is only readable once the whole
                    # reply has arrived.
                    content = _content_text(delta.get("content"))
                    if content:
                        yield TextDelta(content)
                    # `tool_calls` on the delta is where a native call arrives on
                    # a stream, with `content: null` beside it and no trailing
                    # text anywhere.  Read with the same tolerance as the
                    # complete-response path: a null `function`, a missing
                    # `index`, and arguments sent as an object rather than a
                    # string are all shapes real endpoints use, and each of them
                    # used to lose the call entirely.
                    legacy = delta.get("function_call")
                    deltas = delta.get("tool_calls")
                    entries = list(deltas) if isinstance(deltas, list) else []
                    if not entries and isinstance(legacy, dict):
                        entries = [dict(legacy, index=0)]
                    for position, tc in enumerate(entries):
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index", position)
                        if not isinstance(idx, int):
                            idx = position
                        if idx not in acc:
                            acc[idx] = {"id": "", "name": "", "args": ""}
                            order.append(idx)
                        fn = tc.get("function")
                        fn = fn if isinstance(fn, dict) else {}
                        if tc.get("id"):
                            acc[idx]["id"] = str(tc["id"])
                        name = fn.get("name") or tc.get("name")
                        if name:
                            # A tool name arrives whole in the first frame and
                            # absent from the rest -- but some endpoints repeat it
                            # in full every frame, and appending blindly would
                            # publish `clickclick`.  Extending text replaces,
                            # anything else is a fragment and appends.
                            name_text = str(name)
                            known = acc[idx]["name"]
                            if name_text != known:
                                acc[idx]["name"] = (
                                    name_text
                                    if name_text.startswith(known)
                                    else known + name_text
                                )
                        arguments = fn.get("arguments", tc.get("arguments"))
                        if arguments not in (None, ""):
                            acc[idx]["args"] += _arguments_json(arguments)
        # Published after the stream is drained rather than inside it: a caller
        # that abandons the iterator mid-response must not read a half-filled
        # count as if it were the price of the whole request.
        self.last_usage = dict(usage)
        for idx in order:
            a = acc[idx]
            yield ToolCallEvent(
                ToolCall(id=a["id"] or f"call_{idx}", name=a["name"], arguments=a["args"])
            )
        yield Done(stop_reason=stop_reason)
