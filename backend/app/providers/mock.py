from __future__ import annotations

import json
from typing import AsyncIterator, List

from .base import (
    LLMEvent,
    LLMMessage,
    Provider,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSchema,
    Done,
)


class MockProvider(Provider):
    name = "mock"

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        last = messages[-1].content if messages else ""
        lowered = last.lower()
        if lowered.startswith("search "):
            yield ToolCallEvent(
                ToolCall(id="mock_search", name="web_search", arguments=json.dumps({"query": last[7:].strip()}))
            )
            yield Done("tool_calls")
            return
        if lowered.startswith("LOOKUP "):
            yield ToolCallEvent(
                ToolCall(id="mock_lookup", name="shell", arguments=json.dumps({"command": last[7:].strip()}))
            )
            yield Done("tool_calls")
            return
        reply = f"[mock/{model}] received: {last}"
        if messages and messages[0].role == "system" and "Return JSON plan" in messages[0].content:
            reply = '{"objective": "' + last.replace('"', "'")[:120] + '", "steps": ["Analyze the objective"]}'
        yield TextDelta(reply)
        yield Done()