"""An ordinary text request must not inherit the computer-control request shape.

One provider object answers more than one role.  With the default configuration
the Groq provider that drives computer control is also the provider the ordinary
text roles resolve to, and that object was built for the opposite request:
``reasoning_effort: "none"`` and a 512-token ceiling, because a control loop only
ever asks for one small tool call.

That shape is right for the loop and wrong for a text turn.  The GPT-OSS text
models refuse ``"none"`` outright -- the API answers
``reasoning_effort must be one of low, medium, or high`` -- so the first request
Commander made died with a 400 before it could answer at all.  The caller states
the shape it needs per call (``RequestOptions``) and the adapter applies it; a
call with no options keeps the provider exactly as it was configured.

These cases pin the request that actually goes on the wire, because asserting the
settings would pass while the body still carried "none".  The body is read by
substituting a capturing client for ``httpx.AsyncClient`` -- the request is built
in full before any socket is opened, so the bytes under test are the real ones and
nothing is sent.
"""
import asyncio
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.config import Settings
from app.providers import openai_compat
from app.providers.base import LLMMessage, RequestOptions, stream_model
from app.providers.groq import GroqProvider
from app.providers.router import AGENT_ROLES, Router

MODEL = "openai/gpt-oss-120b"

failures = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


class _CapturedRequest(Exception):
    """Raised in place of the socket write, once the body has been read."""


class _CapturingClient:
    """Stands in for ``httpx.AsyncClient`` and records the body it is given.

    Groq uses a complete (non-streamed) response, so the adapter calls
    ``client.post(url, json=body, ...)`` -- the body the test is after.  No
    request leaves the process: the stub raises the moment it has read it.
    """

    last: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url, json=None, headers=None):
        _CapturingClient.last = json
        raise _CapturedRequest


async def capture(provider, model: str, options):
    """The body ``stream_model`` would POST for this shape of call."""
    real_client = openai_compat.httpx.AsyncClient
    _CapturingClient.last = None
    openai_compat.httpx.AsyncClient = _CapturingClient
    try:
        try:
            async for _ in stream_model(
                provider, [LLMMessage("user", "hello")], [], model, options=options
            ):
                pass
        except _CapturedRequest:
            pass
    finally:
        openai_compat.httpx.AsyncClient = real_client
    return _CapturingClient.last


settings = Settings()
settings.agent_provider = "groq"
settings.agent_model = MODEL
settings.agent_reasoning_effort = "low"
settings.agent_max_completion_tokens = 2048

# The provider exactly as computer control builds it: cheapest effort, tiny
# ceiling.  It is the object both roles share, which is the whole bug.
provider = GroqProvider("gsk-test")
router = Router({"groq": provider}, settings)

# --- 1. what each role asks for ------------------------------------------
for role in AGENT_ROLES:
    opts = router.request_options(role)
    check(f"{role} states a request shape", isinstance(opts, RequestOptions), True)
    check(
        f"{role} effort is the configured agent effort",
        getattr(opts, "reasoning_effort", None),
        "low",
    )
    check(
        f"{role} ceiling is the configured agent ceiling",
        getattr(opts, "max_completion_tokens", None),
        2048,
    )

check("computer states no shape (provider as configured)", router.request_options("computer"), None)
check("browser states no shape (provider as configured)", router.request_options("browser"), None)

# --- 2. the bytes on the wire --------------------------------------------
# Both drives share one event loop: the model-call gate is bound to the loop
# that first acquired it, so a second ``asyncio.run`` would be refused.
async def _both():
    return await capture(provider, MODEL, router.request_options("commander")), await capture(
        provider, MODEL, None
    )


text_body, computer_body = asyncio.run(_both())

check(
    "a text turn does not send the computer-control effort",
    text_body.get("reasoning_effort") if text_body else None,
    "low",
)
check(
    "a text turn does not send the computer-control ceiling",
    text_body.get("max_completion_tokens") if text_body else None,
    2048,
)
check(
    "the value a GPT-OSS model rejects is never on the wire",
    (text_body or {}).get("reasoning_effort") in {"low", "medium", "high"},
    True,
)

# Computer control is unchanged: no options means the provider's own values.
check(
    "a computer turn still sends its configured effort",
    computer_body.get("reasoning_effort") if computer_body else None,
    "none",
)
check(
    "a computer turn still sends its configured ceiling",
    computer_body.get("max_completion_tokens") if computer_body else None,
    provider.max_completion_tokens,
)
check(
    "the provider's own values are untouched by a text turn",
    (provider.reasoning_effort, provider.max_completion_tokens),
    ("none", provider.max_completion_tokens),
)

# --- 3. the configured default is itself a legal effort -------------------
# The bug was a caller that forgot to state its shape, so the shape had to
# exist.  A default of "none" here would be the same bug reintroduced.
check(
    "agent_reasoning_effort default is valid for a text model",
    Settings().agent_reasoning_effort in {"low", "medium", "high"},
    True,
)

# --- 4. every agent call site forwards the shape --------------------------
# The route that fails in production is the one this file cannot reach without
# a live model, so the wiring is asserted at the source: the roles above decide
# the shape, and each caller has to pass it on.
CALLERS = {
    "app/agents/loop.py": "options=request_options",
    "app/agents/commander.py": "request_options=",
    "app/agents/routines.py": "request_options=",
    "app/agents/summaries.py": "options=router.request_options",
}
for rel, needle in CALLERS.items():
    path = os.path.join(BACKEND_DIR, *rel.split("/"))
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    check(f"{rel} forwards the request shape", needle in source, True)

print()
if failures:
    print(f"FAIL: {len(failures)} check(s) did not hold")
    for item in failures:
        print(f"  - {item}")
    raise SystemExit(1)
print("PASS: an ordinary text request carries a valid effort, and computer control is unchanged")
