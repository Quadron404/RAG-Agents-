from __future__ import annotations

import json
import re
from typing import AsyncIterator, Callable, List, Optional

from ..providers.base import (
    LLMMessage,
    Provider,
    TextDelta,
    ToolCall,
    ToolCallEvent,
)
from ..tools.base import Tool
from ..tools.executor import Executor

EmitFn = Callable[[dict], object]

TOOLCALL_RE = re.compile(r"\[TOOL\s+([a-zA-Z_]+)\s+(\{.*?\})\s*\]", re.S)


def parse_arguments(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _strip_toolcalls(text: str) -> str:
    cleaned = TOOLCALL_RE.sub("", text)
    lines = [ln.rstrip() for ln in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def extract_text_toolcalls(text: str) -> List[dict]:
    """Parse `[TOOL name {"args"...}]` directives out of model text output."""
    calls = []
    for m in TOOLCALL_RE.finditer(text):
        calls.append({"name": m.group(1), "arguments": m.group(2), "raw": m.group(0)})
    return calls


async def run_agent_loop(
    provider: Provider,
    model: str,
    system: str,
    context: List[LLMMessage],
    user_text: str,
    tools: List[Tool],
    executor: Executor,
    emit: EmitFn,
    agent_name: str,
    max_iters: int,
) -> dict:
    messages: List[LLMMessage] = [LLMMessage("system", system)]
    messages.extend(context)
    messages.append(LLMMessage("user", user_text))

    final_text = ""
    tool_count = 0
    # Aligned model: tools are NOT callable directly — the model only ever emits
    # text (plus tool calls rendered as `[TOOL name {json}]` text). The system parses
    # that text and performs the action on the model's behalf.
    stream_tools: List[Tool] = []

    for _ in range(max_iters):
        tool_calls: List[ToolCall] = []
        turn_text = ""
        async for ev in provider.stream(
            [m for m in messages], [t.schema() for t in stream_tools], model
        ):
            if isinstance(ev, TextDelta):
                turn_text += ev.content
                final_text += ev.content
                await _emit(emit, {"type": "delta", "agent": agent_name, "text": ev.content})
            elif isinstance(ev, ToolCallEvent):
                # Legacy native function-calling still works if a provider forces it.
                tool_calls.append(ev.call)

        directives = extract_text_toolcalls(turn_text)

        if not tool_calls and not directives:
            break

        if not directives:
            messages.append(LLMMessage("assistant", "", tool_calls=tool_calls))
        else:
            messages.append(LLMMessage("assistant", turn_text))

        for directive in directives:
            await _execute(
                executor, emit, messages, agent_name, directive["name"],
                parse_arguments(directive["arguments"]), _new_id("t"),
            )
            tool_count += 1

        for tc in tool_calls:
            args = parse_arguments(tc.arguments)
            tool_count += 1
            await _execute(executor, emit, messages, agent_name, tc.name, args, tc.id)

    return {"text": _strip_toolcalls(final_text), "tool_count": tool_count}


def _new_id(prefix: str) -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _execute(
    executor: Executor,
    emit: EmitFn,
    messages: List[LLMMessage],
    agent_name: str,
    name: str,
    args: dict,
    tool_id: str,
) -> None:
    await _emit(
        emit,
        {"type": "tool_start", "agent": agent_name, "tool": name, "args": args, "id": tool_id},
    )
    output = ""
    error = ""
    image = ""
    async for ev in executor.run(name, args):
        if ev.get("type") == "output":
            partial = ev.get("content", "")
            await _emit(
                emit,
                {"type": "tool_output", "agent": agent_name, "id": tool_id, "content": partial},
            )
        elif ev.get("type") == "result":
            output = ev.get("output", "")
            error = ev.get("error", "")
            image = ev.get("image", "")
    await _emit(
        emit,
        {"type": "tool_result", "agent": agent_name, "id": tool_id, "output": output, "error": error},
    )
    message = LLMMessage(
        "tool", output or error, tool_call_id=tool_id, name=name, images=[image] if image else []
    )
    messages.append(message)


async def _emit(emit: Optional[EmitFn], event: dict) -> None:
    if emit is None:
        return
    if hasattr(emit, "__call__"):
        try:
            result = emit(event)
            import asyncio

            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass