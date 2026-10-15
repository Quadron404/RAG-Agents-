"""The ordinary agent's tools go through the restricted runtime, or nowhere.

Three claims: the unrestricted browser is gone from the registry, ``browse``
never falls back to any other browser whether the runtime is on or off, and the
network tools refuse rather than reach the host when the runtime is not there.
The browser service is not started -- an unreachable service is exactly the
failure a fail-closed design has to get right -- so these are the refusals and
the routing that run without a browser present.
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.agents.commander import WORKER_PROMPT
from app.restricted.guard import Guard
from app.restricted.runtime import RestrictedRuntime
from app.tools.executor import Executor
from app.tools.registry import commander_tools, worker_tools

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


def collect(agen):
    async def run():
        out = []
        async for ev in agen:
            out.append(ev)
        return out

    return asyncio.run(run())


# --- the registry no longer offers an unrestricted browser ---------------
worker_names = {t.name for t in worker_tools()}
check("the ordinary-agent registry offers browse", "browse" in worker_names)
check("the ordinary-agent registry no longer offers browser", "browser" not in worker_names)
check("the commander registry matches the worker registry",
      {t.name for t in commander_tools()} == worker_names)
check("the browse tool's description names the allowlist", "allowlist" in next(
    t for t in worker_tools() if t.name == "browse").description.lower())
check("the worker prompt points at browse, not a persistent browser",
      "browse" in WORKER_PROMPT and "persistent browser" not in WORKER_PROMPT)

# --- a policy file with one approved host --------------------------------
tmp = tempfile.mkdtemp(prefix="rag-restricted-tools-")
allowlist = os.path.join(tmp, "allowlist")
with open(allowlist, "w", encoding="utf-8") as fh:
    fh.write("allowed.example\n")

settings = SimpleNamespace(
    restricted_enabled=True,
    restricted_allowlist_path=allowlist,
    restricted_gateway_url="http://127.0.0.1:9111",
    restricted_browser_url="http://127.0.0.1:59999",  # deliberately not listening
    restricted_shell_url="",
    max_web_chars=12000,
)


def _resolver(host):
    # A real resolver is injected so the guard's shape check passes for the one
    # approved host without depending on DNS being up in CI.
    if host == "allowed.example":
        return ["93.184.216.34"]
    raise OSError(f"no such host: {host}")

runtime = RestrictedRuntime(settings, guard=Guard(resolve=_resolver))
executor = Executor(settings, restricted=runtime)

# --- browse refuses an unlisted destination without any network ----------
events = collect(executor.run("browse", {"action": "goto", "url": "http://evil.example/"}))
check("browse refuses an unlisted destination", events and "refused" in str(events[0].get("error", "")).lower(),
      events)

# --- browse fails closed when the service is down ------------------------
events = collect(executor.run("browse", {"action": "goto", "url": "https://allowed.example/"}))
check("browse fails closed when the browser service is unreachable",
      events and "unavailable" in str(events[0].get("error", "")).lower(), events)

# --- fetch refuses an unlisted destination -------------------------------
events = collect(executor.run("fetch_url", {"url": "http://evil.example/"}))
check("fetch_url refuses an unlisted destination", events and "refused" in str(events[0].get("error", "")).lower(),
      events)

# --- search is subject to the same allowlist -----------------------------
events = collect(executor.run("web_search", {"query": "anything"}))
check("web_search is refused when its endpoint is not allowlisted",
      events and "refused" in str(events[0].get("error", "")).lower(), events)

# --- shell fails closed with no sandbox ----------------------------------
events = collect(executor.run("shell", {"command": "curl http://evil.example/"}))
check("shell refuses when no sandbox is deployed", events and "disabled" in str(events[0].get("error", "")).lower(),
      events)

# --- with the runtime off, browse still never reaches a browser ----------
off = SimpleNamespace(restricted_enabled=False, max_web_chars=12000)
executor_off = Executor(off, restricted=RestrictedRuntime(off))
events = collect(executor_off.run("browse", {"action": "goto", "url": "https://allowed.example/"}))
check("browse is refused, not rerouted, when restricted browsing is off",
      events and "disabled" in str(events[0].get("error", "")).lower(), events)

# --- the policy file is the source of truth ------------------------------
with open(allowlist, "w", encoding="utf-8") as fh:
    fh.write("other.example\n")
events = collect(executor.run("browse", {"action": "goto", "url": "https://allowed.example/"}))
check("an edit to the allowlist takes away a destination without a restart",
      events and "refused" in str(events[0].get("error", "")).lower(), events)

print()
print("all restricted-tool checks pass" if not failures else f"{len(failures)} tool check(s) failed")
sys.exit(1 if failures else 0)
