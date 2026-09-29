from __future__ import annotations

from typing import Dict, Tuple

from ..config import Settings


class ProviderUnavailable(RuntimeError):
    """A role was asked for a provider or model that is not configured.

    Distinct from a missing provider in the other roles, where falling back to
    the mock is a deliberate convenience.  For computer control a fallback would
    be a lie: the loop would report progress it never made.
    """


class Router:
    def __init__(self, providers: Dict[str, object], settings: Settings):
        self.providers = providers
        self.settings = settings

    def resolve(self, role: str) -> Tuple[object, str]:
        mapping = {
            "commander": (self.settings.commander_provider, self.settings.commander_model),
            "worker": (self.settings.worker_provider, self.settings.worker_model),
            "browser": (self.settings.browser_provider, self.settings.browser_model),
            "computer": (self.settings.computer_provider, self.settings.computer_model),
        }
        provider_name, model = mapping.get(role, (self.settings.worker_provider, self.settings.worker_model))
        provider = self.providers.get(provider_name)
        # The computer role is the one place where silently falling back is not
        # allowed.  A mock provider would answer a screenshot-driven task with
        # invented commands and the loop would "work" while touching nothing, so
        # a missing provider or a missing model is raised instead -- the caller
        # turns that into an explicit "no API key configured" failure.
        if role == "computer":
            if provider is None:
                raise ProviderUnavailable(
                    f"provider {provider_name!r} is not configured; "
                    "set OPENROUTER_API_KEY and OPENROUTER_MODEL"
                )
            if not model:
                raise ProviderUnavailable("no computer model configured; set OPENROUTER_MODEL")
            return provider, model
        if provider is None:
            provider = self.providers["mock"]
            model = "mock"
        return provider, model