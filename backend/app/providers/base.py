from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Dict, List, Optional, Union


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

    async def stream(
        self, messages: List[LLMMessage], tools: List[ToolSchema], model: str
    ) -> AsyncIterator[LLMEvent]:
        raise NotImplementedError


MODEL_CALL_GAP_SECONDS = 10.0


class _ModelCallGate:
    """Serialize external model calls and enforce a hard 10s post-call gap.

    The lock is held for the complete provider stream, not merely for opening
    the HTTP connection. Therefore two model calls can never overlap, and the
    next call cannot start until ten seconds after the previous call finished
    (successful, failed, or rate-limited).
    """

    def __init__(self) -> None:
        import asyncio
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def __aenter__(self):
        import asyncio
        await self._lock.acquire()
        try:
            wait = self._next_allowed - asyncio.get_running_loop().time()
            if wait > 0:
                await asyncio.sleep(wait)
            return self
        except Exception:
            self._lock.release()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        import asyncio
        self._next_allowed = asyncio.get_running_loop().time() + MODEL_CALL_GAP_SECONDS
        self._lock.release()
        return False


_MODEL_CALL_GATE = _ModelCallGate()


async def stream_model(
    provider: "Provider",
    messages: List[LLMMessage],
    tools: List[ToolSchema],
    model: str,
    *,
    on_rate_limit: Optional[Callable[[Exception], object]] = None,
) -> AsyncIterator[LLMEvent]:
    """Run one provider request under the global 10s gate.

    HTTP 429 is special: the current request is *stalled*, not failed. The same
    exact request is retried only after the mandatory ten-second gate expires.
    No newer request gets to overtake it in a serialized higher-level run.
    """
    from inspect import isawaitable
    from .errors import ProviderHTTPError

    while True:
        try:
            async with _MODEL_CALL_GATE:
                async for event in provider.stream(messages, tools, model):
                    yield event
            return
        except ProviderHTTPError as exc:
            if exc.status != 429:
                raise
            if on_rate_limit is not None:
                note = on_rate_limit(exc)
                if isawaitable(note):
                    await note
            # Do not sleep here. Exiting the gate already starts the exact
            # ten-second timer. Re-entering the gate waits for its expiry.
            continue


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

    Never includes the base64 itself, the API key, or any header value.
    """
    wire_messages = body.get("messages") or []
    if not isinstance(wire_messages, list):
        wire_messages = []

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
    }
