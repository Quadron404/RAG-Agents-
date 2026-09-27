from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional, Union


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


class Provider:
    name: str = "provider"

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        raise NotImplementedError


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