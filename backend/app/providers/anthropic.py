from __future__ import annotations

import json
from typing import AsyncIterator, List

import httpx

from .base import (
    LLMEvent,
    LLMMessage,
    Provider,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    image_mime,
    tool_schema_openai,
    truncate,
)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str, version: str = "2023-06-01", timeout: float = 180.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.timeout = timeout

    def _wire_messages(self, messages: List[LLMMessage]):
        system = None
        out = []
        for m in messages:
            if m.role == "system":
                system = m.content or None
            elif m.role == "user":
                if m.content:
                    out.append({"role": "user", "content": [{"type": "text", "text": m.content}]})
            elif m.role == "assistant":
                parts = []
                if m.content:
                    parts.append({"type": "text", "text": m.content})
                for tc in m.tool_calls or []:
                    try:
                        inp = json.loads(tc.arguments or "{}")
                    except json.JSONDecodeError:
                        inp = {}
                    parts.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": inp})
                if parts:
                    out.append({"role": "assistant", "content": parts})
            elif m.role == "tool":
                tool_parts = [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": []}]
                content_parts = []
                if m.content:
                    content_parts.append({"type": "text", "text": m.content})
                for img in m.images or []:
                    if img:
                        content_parts.append(
                            {"type": "image", "source": {"type": "base64", "media_type": image_mime(img), "data": img}}
                        )
                tool_parts[0]["content"] = content_parts
                out.append({"role": "user", "content": tool_parts})
        return system, out

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        system, wire = self._wire_messages(messages)
        if not wire:
            wire = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
        body = {"model": model, "max_tokens": 8192, "messages": wire, "stream": True}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
                }
                for t in tools
            ]
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
            "content-type": "application/json",
        }
        timeout = httpx.Timeout(self.timeout, connect=15.0)
        acc: dict = {}
        order: list = []
        evt = ""
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        evt = line[7:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if evt == "content_block_start":
                        cb = obj.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            idx = obj.get("index", 0)
                            acc[idx] = {"id": cb.get("id", ""), "name": cb.get("name", ""), "args": ""}
                            order.append(idx)
                    elif evt == "content_block_delta":
                        d = obj.get("delta", {})
                        if d.get("type") == "text_delta":
                            yield TextDelta(d.get("text", ""))
                        elif d.get("type") == "input_json_delta":
                            idx = obj.get("index", 0)
                            acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                            acc[idx]["args"] += d.get("partial_json", "")
                    elif evt == "message_stop":
                        break
        for idx in order:
            a = acc[idx]
            yield ToolCallEvent(ToolCall(id=a["id"], name=a["name"], arguments=a["args"] or "{}"))
        yield Done()