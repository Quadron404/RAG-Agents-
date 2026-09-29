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
    tool_schema_openai,
    truncate,
)


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, api_key: str, base_url: str, timeout: float = 180.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _wire_messages(messages: List[LLMMessage]):
        system = None
        contents = []
        for m in messages:
            if m.role == "system":
                system = m.content or None
            elif m.role == "user":
                if m.content:
                    contents.append({"role": "user", "parts": [{"text": m.content}]})
            elif m.role == "assistant":
                parts = []
                if m.content:
                    parts.append({"text": m.content})
                for tc in m.tool_calls or []:
                    try:
                        args = json.loads(tc.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    parts.append({"functionCall": {"name": tc.name, "args": args}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif m.role == "tool":
                try:
                    response = json.loads(m.content or "{}")
                except json.JSONDecodeError:
                    response = {"content": m.content}
                func_part = {"functionResponse": {"name": m.name, "response": response}}
                parts = [func_part]
                for img in m.images or []:
                    if img:
                        parts.append({"inline_data": {"mime_type": image_mime(img), "data": img}})
                contents.append({"role": "user", "parts": parts})
        merged = []
        for c in contents:
            if merged and merged[-1]["role"] == c["role"]:
                merged[-1]["parts"].extend(c["parts"])
            else:
                merged.append(c)
        return system, merged

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        system, contents = self._wire_messages(messages)
        url = f"{self.base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        body = {
            "contents": contents or [{"role": "user", "parts": [{"text": ""}]}],
            "generationConfig": {"maxOutputTokens": 8192},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["function"]["name"],
                            "description": t["function"].get("description", ""),
                            "parameters": t["function"].get("parameters", {"type": "object", "properties": {}}),
                        }
                        for t in tools
                    ]
                }
            ]
        headers = {"x-goog-api-key": self.api_key}
        timeout = httpx.Timeout(self.timeout, connect=15.0)
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
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    candidates = obj.get("candidates") or []
                    if not candidates:
                        continue
                    first = candidates[0]
                    for part in (first.get("content") or {}).get("parts") or []:
                        if part.get("text"):
                            yield TextDelta(part["text"])
                        if part.get("functionCall"):
                            fc = part["functionCall"]
                            args = fc.get("args") or {}
                            yield ToolCallEvent(
                                ToolCall(id=f"fc_{fc['name']}", name=fc["name"], arguments=json.dumps(args))
                            )
                    if first.get("finishReason"):
                        yield Done(first["finishReason"])
        yield Done("stop")