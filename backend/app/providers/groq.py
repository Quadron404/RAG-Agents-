from __future__ import annotations

from .openai_compat import OpenAICompatProvider

#: Used when GROQ_MODEL is unset.  Spelled the way Groq names it on its own
#: model list, so a run names the same model the API knows rather than one this
#: file invented.  A vision-capable model is the default on purpose: the loop
#: sends a screenshot on every turn after the first, and a text-only model
#: cannot answer that with anything but a guess.
DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"


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

    def __init__(self, api_key: str, base_url: str = "https://api.groq.com/openai/v1", timeout: float = 180.0):
        super().__init__("groq", api_key, base_url, timeout=timeout)

    @property
    def default_model(self) -> str:
        return DEFAULT_GROQ_MODEL


__all__ = ["GroqProvider", "DEFAULT_GROQ_MODEL"]