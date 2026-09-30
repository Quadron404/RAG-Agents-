from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..config import Settings


class ProviderUnavailable(RuntimeError):
    """A role was asked for a provider or model that is not configured.

    Distinct from a missing provider in the other roles, where falling back to
    the mock is a deliberate convenience.  For computer control a fallback would
    be a lie: the loop would report progress it never made.
    """


@dataclass(frozen=True)
class ComputerProviderInfo:
    """What the browser is allowed to know about a computer-control provider.

    Configured state and the model name, never the key.  This is the whole of
    what the provider selector needs, and it is deliberately the whole of it:
    adding the key here would put a secret in the frontend bundle's reach for
    no benefit, since the browser cannot use a key it must not see.
    """

    name: str
    label: str
    model: str
    configured: bool


#: The providers a computer-control run may be pointed at, in the order the
#: selector lists them.  A closed list rather than whatever happens to be in
#: `providers`: the browser can only be offered a computer provider that knows
#: its own model name, and a provider that has no entry here has none.
COMPUTER_PROVIDER_ORDER = ("openrouter", "mistral")


def computer_provider_info(settings: Settings, name: str) -> ComputerProviderInfo:
    label, model, configured = {
        "openrouter": (
            "OpenRouter",
            settings.computer_model or "",
            bool(settings.openrouter_api_key),
        ),
        "mistral": (
            "Mistral",
            settings.mistral_model,
            bool(settings.mistral_api_key),
        ),
    }.get(name, (name, "", False))
    return ComputerProviderInfo(name=name, label=label, model=model, configured=configured)


def computer_providers(settings: Settings) -> List[ComputerProviderInfo]:
    return [computer_provider_info(settings, name) for name in COMPUTER_PROVIDER_ORDER]


def computer_model_for(settings: Settings, name: str) -> str:
    """The model this provider should be asked for.

    Mistral defaults to a vision-capable model because a key with no model
    named is a missing variable rather than a request for a text-only model,
    and computer control without an image is not computer control.
    """
    if name == "mistral":
        return settings.mistral_model or "mistral-small-2506"
    return settings.computer_model or ""


class Router:
    def __init__(self, providers: Dict[str, object], settings: Settings):
        self.providers = providers
        self.settings = settings

    def resolve(self, role: str, provider_name: Optional[str] = None) -> Tuple[object, str]:
        mapping = {
            "commander": (self.settings.commander_provider, self.settings.commander_model),
            "worker": (self.settings.worker_provider, self.settings.worker_model),
            "browser": (self.settings.browser_provider, self.settings.browser_model),
            "computer": (
                provider_name or self.settings.computer_provider,
                None,
            ),
        }
        chosen, model = mapping.get(role, (self.settings.worker_provider, self.settings.worker_model))
        if role == "computer":
            # Resolved per call rather than from settings, because a run can be
            # pointed at a different provider between turns.  The model comes
            # from the provider's own configuration, so "OpenRouter" and
            # "Mistral" cannot end up sharing one model name.
            model = computer_model_for(self.settings, chosen)
        provider = self.providers.get(chosen)
        # The computer role is the one place where silently falling back is not
        # allowed.  A mock provider would answer a screenshot-driven task with
        # invented commands and the loop would "work" while touching nothing, so
        # a missing provider or a missing model is raised instead -- the caller
        # turns that into an explicit "not configured" failure naming the
        # provider that failed, because "the AI stopped" is not a diagnosis.
        if role == "computer":
            info = computer_provider_info(self.settings, chosen)
            # Presence in `providers` is the check, not the settings key.
            # `build_providers` only registers a provider when its key was
            # supplied, so the dict already answers "can this actually make a
            # call" -- and it stays right for a provider wired up in code, which
            # is how the tests construct one.  The settings-derived
            # `configured` flag is for the browser's selector and nothing else.
            if provider is None:
                raise ProviderUnavailable(f"{info.label} is not configured")
            if not model:
                raise ProviderUnavailable(f"{info.label} is not configured: no model configured")
            return provider, model
        if provider is None:
            provider = self.providers["mock"]
            model = "mock"
        return provider, model
