"""Ordinary-agent routing, and the line it must not cross.

Every text role (commander, worker, email, summary) resolves through the shared
``agent_provider``/``agent_model`` pair, and ``summary`` deliberately resolves to
the smaller fallback model because compression should not spend the large
model's budget.  Computer control is not a text role and must keep resolving
through its own ``COMPUTER_PROVIDER``/``*_MODEL`` settings: wiring ordinary
agents must never silently change which model drives a real browser.

These cases pin both halves, because the failure that matters is the second one
-- computer control quietly re-pointed at a text model -- and it is invisible in
a passing agent test unless it is asserted here.
"""
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.config import Settings
from app.providers.mock import MockProvider
from app.providers.router import AGENT_ROLES, ProviderUnavailable, Router


class _RealProvider:
    """Stands in for a registered, keyed provider (Groq/OpenRouter/etc.)."""

    def __init__(self, name: str):
        self.name = name
        self.last_usage = None


failures = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


settings = Settings()
settings.agent_provider = "groq"
settings.agent_model = "openai/gpt-oss-120b"
settings.agent_fallback_model = "openai/gpt-oss-20b"
settings.computer_provider = "openrouter"
settings.computer_model = "cc-vision-model"

groq = _RealProvider("groq")
openrouter = _RealProvider("openrouter")
mock = MockProvider()
providers = {"mock": mock, "groq": groq, "openrouter": openrouter}
router = Router(providers, settings)

check("commander -> agent model", router.resolve("commander"), (groq, "openai/gpt-oss-120b"))
check("worker -> agent model", router.resolve("worker"), (groq, "openai/gpt-oss-120b"))
check("email -> agent model", router.resolve("email"), (groq, "openai/gpt-oss-120b"))
check("summary -> fallback model", router.resolve("summary"), (groq, "openai/gpt-oss-20b"))

check(
    "worker attempts primary then fallback",
    router.resolve_attempts("worker"),
    [(groq, "openai/gpt-oss-120b"), (groq, "openai/gpt-oss-20b")],
)
check(
    "summary has a single attempt (fallback already in use)",
    router.resolve_attempts("summary"),
    [(groq, "openai/gpt-oss-20b")],
)

# Computer control is untouched: its provider comes from COMPUTER_PROVIDER and
# its model from that provider's own setting, never from agent_*.
check("computer provider is not the agent provider", router.resolve("computer"), (openrouter, "cc-vision-model"))
check("computer is not an agent role", router.is_agent_role("computer"), False)
check("browser is not an agent role", router.is_agent_role("browser"), False)
for role in AGENT_ROLES:
    check(f"{role} is an agent role", router.is_agent_role(role), True)

# With no real provider registered, a text role falls back to the mock, and a
# mock has no meaningful fallback attempt -- there is nothing to downgrade to.
bare = Router({"mock": mock}, settings)
check("unregistered agent provider -> mock", bare.resolve("commander"), (mock, "mock"))
check("mock role has no fallback attempt", bare.resolve_attempts("worker"), [(mock, "mock")])

# Computer control must refuse rather than fall back to a mock: a mock would
# answer a screenshot task with invented commands.
try:
    Router({"mock": mock}, settings).resolve("computer")
    check("missing computer provider raises", "no exception", "ProviderUnavailable")
except ProviderUnavailable as exc:
    check("missing computer provider raises", "ProviderUnavailable", "ProviderUnavailable")
    print(f"     ({exc})")

print()
if failures:
    print(f"{len(failures)} routing check(s) failed")
    sys.exit(1)
print("all agent routing checks pass")
