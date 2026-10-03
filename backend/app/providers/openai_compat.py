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
        self.last_usage = {}
        async with httpx.AsyncClient(timeout=timeout) as client:
            if not use_stream:
                resp = await client.post("POST", url, json=body, headers=headers)
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
                reported = payload.get("usage")
                if isinstance(reported, dict):
                    usage = {
                        "prompt_tokens": int(reported.get("prompt_tokens") or 0),
                        "completion_tokens": int(reported.get("completion_tokens") or 0),
                        "total_tokens": int(reported.get("total_tokens") or 0),
                    }
                choices = payload.get("choices") or []
                first = choices[0] if choices and isinstance(choices[0], dict) else {}
                message = first.get("message") if isinstance(first, dict) else {}
                if not isinstance(message, dict):
                    message = {}
                content = message.get("content")
                if content:
                    yield TextDelta(str(content))
                for tc in message.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    yield ToolCallEvent(
                        ToolCall(
                            id=str(tc.get("id") or "call_0"),
                            name=str(fn.get("name") or ""),
                            arguments=str(fn.get("arguments") or "{}"),
                        )
                    )
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
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield TextDelta(content)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        if idx not in acc:
                            acc[idx] = {"id": "", "name": "", "args": ""}
                            order.append(idx)
                        fn = tc.get("function", {})
                        if tc.get("id"):
                            acc[idx]["id"] = tc["id"]
                        if fn.get("name"):
                            acc[idx]["name"] += fn["name"]
                        if fn.get("arguments"):
                            acc[idx]["args"] += fn["arguments"]
        # Published after the stream is drained rather than inside it: a caller
        # that abandons the iterator mid-response must not read a half-filled
        # count as if it were the price of the whole request.
        self.last_usage = dict(usage)
        for idx in order:
            a = acc[idx]
            yield ToolCallEvent(
                ToolCall(id=a["id"] or f"call_{idx}", name=a["name"], arguments=a["args"])
            )
        yield Done()