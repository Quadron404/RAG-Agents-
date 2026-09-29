from __future__ import annotations

import os
from typing import Dict

from ..config import Settings
from .anthropic import AnthropicProvider
from .gemini import GeminiProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider


def build_providers(settings: Settings) -> Dict[str, object]:
    providers: Dict[str, object] = {
        "mock": MockProvider(),
    }
    if os.environ.get("OPENAI_API_KEY"):
        providers["openai"] = OpenAICompatProvider(
            "openai", os.environ["OPENAI_API_KEY"], os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        )
    if os.environ.get("XAI_API_KEY"):
        providers["grok"] = OpenAICompatProvider(
            "grok", os.environ["XAI_API_KEY"], os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        providers["anthropic"] = AnthropicProvider(
            os.environ["ANTHROPIC_API_KEY"], os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        )
    if os.environ.get("GOOGLE_API_KEY"):
        providers["gemini"] = GeminiProvider(
            "gemini", os.environ["GOOGLE_API_KEY"], os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
        )
    # OpenRouter speaks the OpenAI chat/completions wire format, so it reuses
    # that adapter with a different base URL rather than growing a provider that
    # differs only in its hostname.  It is registered from the Settings object
    # rather than from os.environ so computer control can name it as a role.
    if settings.openrouter_api_key:
        providers["openrouter"] = OpenAICompatProvider(
            "openrouter",
            settings.openrouter_api_key,
            settings.openrouter_base_url,
        )
    return providers