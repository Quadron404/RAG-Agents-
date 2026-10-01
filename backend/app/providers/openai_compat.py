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
    def __init__(self, name: str, api_key: str, base_url: str, timeout: float = 180.0):
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
                parts = []
                if m.content:
                    parts.append({"type": "text", "text": m.content})
                for img in m.images or []:
                    if img:
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{image_mime(img)};base64,{img}", "detail": "high"},
                            }
                        )
                content = parts if parts else ""
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": content})
        return out

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        url = f"{self.base_url}/chat/completions"
        body = {"model": model, "messages": self._wire_messages(messages), "stream": True}
        if tools:
            body["tools"] = tools
        # Recorded from the body that is about to go out, before it is sent, so
        # the inspector shows what the API was actually given.  Headers and the
        # key are not part of it, and the base64 is not copied into the summary.
        self.last_wire = summarize_wire(body, messages, path="/chat/completions")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = httpx.Timeout(self.timeout, connect=15.0)
        acc: dict = {}
        order: list = []
        async with httpx.AsyncClient(timeout=timeout) as client:
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
        for idx in order:
            a = acc[idx]
            yield ToolCallEvent(
                ToolCall(id=a["id"] or f"call_{idx}", name=a["name"], arguments=a["args"])
            )
        yield Done()