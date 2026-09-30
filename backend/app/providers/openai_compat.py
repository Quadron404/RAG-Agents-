from __future__ import annotations

import json
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
                resp.raise_for_status()
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