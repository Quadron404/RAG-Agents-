from __future__ import annotations

from typing import Dict, Tuple

from ..config import Settings


class Router:
    def __init__(self, providers: Dict[str, object], settings: Settings):
        self.providers = providers
        self.settings = settings

    def resolve(self, role: str) -> Tuple[object, str]:
        mapping = {
            "commander": (self.settings.commander_provider, self.settings.commander_model),
            "worker": (self.settings.worker_provider, self.settings.worker_model),
            "browser": (self.settings.browser_provider, self.settings.browser_model),
        }
        provider_name, model = mapping.get(role, (self.settings.worker_provider, self.settings.worker_model))
        provider = self.providers.get(provider_name)
        if provider is None:
            provider = self.providers["mock"]
            model = "mock"
        return provider, model