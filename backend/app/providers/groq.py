from __future__ import annotations

from .openai_compat import OpenAICompatProvider

#: Used when GROQ_MODEL is unset.  Spelled the way Groq names it on its own
#: model list, so a run names the same model the API knows rather than one this
#: file invented.  A vision-capable model is the default on purpose: the model
#: has to read the screen when it asks to, and a text-only model can only
#: describe a page it was never shown.
DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"

#: Completion ceiling for a single tool call.  Sized to a schema-shaped object
#: and nothing else: a screenshot annotation or a history line is a sentence,
#: not an essay, so anything past this is the model narrating instead of acting.
GROQ_MAX_COMPLETION_TOKENS = 256


class GroqProvider(OpenAICompatProvider):
    """Groq as a computer-control provider.

    Groq serves an OpenAI-compatible ``/chat/completions``: the same request
    body, the same content-part list, the same ``data:`` streaming deltas.  So
    this subclasses the OpenAI-compatible adapter rather than copying it, for
    the same reason Mistral does -- a copy would be a second implementation of
    the one thing that must not differ between providers, which is how a
    screenshot becomes an image part.  Error handling (status line, streamed
    error body, ``Retry-After``, the ``ProviderHTTPError`` the runner reports)
    therefore comes along unchanged, and Groq's 429s and 5xxs are handled by
    the code path already written and tested for the other two.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 180.0,
        max_completion_tokens: int = GROQ_MAX_COMPLETION_TOKENS,
        reasoning_effort: str = "none",
    ):
        # The two limits are set here rather than left to the shared adapter's
        # defaults because they are the whole point of choosing Groq: it is the
        # cheapest way to run a loop that only ever asks for one tool call at a
        # time, and that is only true while each call stays small.  Groq meters
        # tokens per second, so a call that thinks out loud also holds the
        # token pool that the next thirty turns need.
        super().__init__(
            "groq",
            api_key,
            base_url,
            timeout=timeout,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
        )

    @property
    def default_model(self) -> str:
        return DEFAULT_GROQ_MODEL


__all__ = ["GroqProvider", "DEFAULT_GROQ_MODEL", "GROQ_MAX_COMPLETION_TOKENS"]
