from __future__ import annotations

from typing import List

from .base import LLMMessage
from .openai_compat import OpenAICompatProvider

#: Used when MISTRAL_MODEL is unset.  mistral-small-2506 takes image parts, so
#: a run does not have to be told to fall back to a text-only model the moment
#: somebody omits the variable.
DEFAULT_MISTRAL_MODEL = "mistral-small-2506"


class MistralProvider(OpenAICompatProvider):
    """Mistral as a computer-control provider.

    Mistral speaks the OpenAI chat/completions wire format -- same endpoint
    shape, same content-part list, same streaming deltas -- so this subclasses
    the OpenAI-compatible adapter rather than copying it.  A copy would be a
    second implementation of the one thing that must be identical between the
    two providers: how a screenshot becomes an image part.  If the two ever
    diverged there, the loop would behave differently depending on which
    provider was selected, which is precisely the bug this design is avoiding.

    So the only thing overridden is the part that genuinely differs, and it is
    a deletion.  OpenAI's `detail: "high"` is a hint about token spend; Mistral
    rejects the field on an image part.  Dropping it costs nothing and keeps
    the request valid on the endpoint that is actually being called.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.mistral.ai/v1", timeout: float = 180.0,
                 max_completion_tokens: int = 0, reasoning_effort: str = ""):
        super().__init__(
            "mistral", api_key, base_url, timeout=timeout,
            max_completion_tokens=max_completion_tokens, reasoning_effort=reasoning_effort,
        )
        # Same ceiling, Mistral's name for it.  Sending OpenAI's spelling to a
        # Mistral endpoint that does not recognise it fails the whole request,
        # which would mean a cost control could take a provider offline.
        self.token_limit_field = "max_tokens"

    @property
    def default_model(self) -> str:
        return DEFAULT_MISTRAL_MODEL

    def _wire_messages(self, messages: List[LLMMessage]) -> list:
        wire = super()._wire_messages(messages)
        out: list = []
        for message in wire:
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        part["image_url"].pop("detail", None)
            # Mistral's tool result is a string: it accepts a screenshot in a
            # user turn but not inside the tool message that reports one.  So an
            # image riding on a tool result is lifted into the next user turn and
            # the tool result keeps its text.  The call still has its own tool
            # result, which is what the endpoint validates against -- the image
            # simply arrives one message later than OpenAI would deliver it.
            if message.get("role") == "tool" and isinstance(content, list):
                images = [p for p in content if p.get("type") == "image_url"]
                if images:
                    words = [p["text"] for p in content if p.get("type") == "text"]
                    message = dict(message, content=" ".join(words) or "ok")
                    out.append(message)
                    out.append({"role": "user", "content": images})
                    continue
            out.append(message)
        return out


__all__ = ["MistralProvider", "DEFAULT_MISTRAL_MODEL"]
