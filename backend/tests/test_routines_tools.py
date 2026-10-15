"""The routine tools an ordinary agent can call.

What matters, and what a naive version would get wrong:

* the nine tools exist in the ordinary-agent registry;
* ``get_current_time`` answers from a real clock with no user turn;
* ownership comes from the bound identity, never from tool arguments -- a
  ``create_routine`` call is executed with the signed-in user's ``owner_sub``
  whatever the model puts in the arguments;
* the flat tool fields are packed into the schedule the edge validates;
* an ambiguous schedule is surfaced as a question, not swallowed;
* failures are classified so the scheduler delays on a rate limit and fails on a
  bad request.

The edge itself is stubbed: these are the tools' own behaviour.  The edge's
recurrence and run ledger are tested in the Worker's own suite.
"""
import asyncio
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.agents.routines import classify_failure
from app.config import Settings
from app.providers.errors import ModelCallGateTimeout, ProviderHTTPError
from app.providers.router import ProviderUnavailable
from app.routines import context as routine_context
from app.routines import tools as T
from app.routines.context import RoutineContext
from app.tools.registry import worker_tools

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


settings = Settings()
settings.worker_origin = "https://edge.test"
settings.rag_internal_token = "tok"


# --- registry ---------------------------------------------------------------
names = {t.name for t in worker_tools()}
expected = {
    "get_current_time", "create_routine", "list_routines", "get_routine",
    "update_routine", "pause_routine", "resume_routine", "delete_routine",
    "get_routine_run_status",
}
check("all routine tools are in the ordinary-agent registry", expected <= names, sorted(expected - names))

# --- get_current_time needs no user turn ------------------------------------
out = asyncio.run(T.run_routine_tool("get_current_time", {}, settings))
check("get_current_time answers with a real timestamp", out["type"] == "result" and "UTC" in out["output"], out)
check("get_current_time reported the current year", "20" in out["output"])


# --- a stubbed edge ---------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.calls = []
        self.routine = {
            "routine_id": "r-1",
            "name": "Inbox digest",
            "schedule_text": "every day at 09:00 (Europe/Paris)",
            "timezone": "Europe/Paris",
            "enabled": 1,
            "next_run_at": 1_700_000_000_000,
            "last_run_status": "",
        }
        self.override = None

    def _body(self):
        return self.override or {"ok": True, "routine": self.routine}

    async def create(self, payload):
        self.calls.append(("create", payload))
        return {"status": 200, "body": self._body()}

    async def list_for(self, owner_sub):
        self.calls.append(("list", owner_sub))
        return {"status": 200, "body": {"ok": True, "routines": [self.routine]}}

    async def get(self, routine_id, owner_sub):
        self.calls.append(("get", routine_id, owner_sub))
        return {"status": 200, "body": self._body()}

    async def update(self, routine_id, patch, owner_sub):
        self.calls.append(("update", routine_id, patch, owner_sub))
        return {"status": 200, "body": self._body()}

    async def pause(self, routine_id, owner_sub):
        self.calls.append(("pause", routine_id, owner_sub))
        return {"status": 200, "body": self._body()}

    async def resume(self, routine_id, owner_sub):
        self.calls.append(("resume", routine_id, owner_sub))
        return {"status": 200, "body": self._body()}

    async def delete(self, routine_id, owner_sub):
        self.calls.append(("delete", routine_id, owner_sub))
        return {"status": 200, "body": {"ok": True}}

    async def runs(self, routine_id, owner_sub, limit=20):
        self.calls.append(("runs", routine_id, owner_sub, limit))
        return {
            "status": 200,
            "body": {
                "ok": True,
                "runs": [
                    {"status": "completed", "attempt_count": 1, "scheduled_for": 1_700_000_000_000,
                     "result_summary": "sent the digest"}
                ],
            },
        }


fake = FakeClient()
T.RoutineClient = type("FakeFactory", (), {"from_settings": staticmethod(lambda _s: fake)})


def with_context(fn):
    token = routine_context.set_current(
        RoutineContext(
            user_id="uid-1",
            google_sub="google-sub-1",
            agent_id="commander",
            conversation_id="conv-1",
            timezone="Europe/Paris",
        )
    )
    try:
        return asyncio.run(fn())
    finally:
        routine_context.reset_current(token)


# --- ownership comes from the bound identity -------------------------------
result = with_context(lambda: T.run_routine_tool(
    "create_routine",
    {
        "name": "Inbox digest",
        "task_prompt": "Summarise the inbox",
        "schedule_type": "weekly",
        "time": "9:00",
        "weekdays": [1, 3],
        "owner_sub": "attacker",  # must be ignored
    },
    settings,
))
payload = fake.calls[-1][1]
check("create_routine sends the bound owner_sub, not the argument",
      payload.get("owner_sub") == "google-sub-1", payload.get("owner_sub"))
check("create_routine packs a weekly schedule",
      payload.get("schedule") == {"time": "9:00", "weekdays": [1, 3]}, payload.get("schedule"))
check("create_routine defaults the timezone to the owner's",
      payload.get("timezone") == "Europe/Paris", payload.get("timezone"))
check("create_routine reports success", result["output"].startswith("Created:"), result)

# --- other tools route to the right edge call -------------------------------
with_context(lambda: T.run_routine_tool("list_routines", {}, settings))
check("list_routines asks the edge for the owner's routines", fake.calls[-1] == ("list", "google-sub-1"))

with_context(lambda: T.run_routine_tool("pause_routine", {"routine_id": "r-1"}, settings))
check("pause_routine pauses by id", fake.calls[-1] == ("pause", "r-1", "google-sub-1"))

with_context(lambda: T.run_routine_tool("resume_routine", {"routine_id": "r-1"}, settings))
check("resume_routine resumes by id", fake.calls[-1] == ("resume", "r-1", "google-sub-1"))

with_context(lambda: T.run_routine_tool("delete_routine", {"routine_id": "r-1"}, settings))
check("delete_routine deletes by id", fake.calls[-1] == ("delete", "r-1", "google-sub-1"))

runs_out = with_context(lambda: T.run_routine_tool("get_routine_run_status", {"routine_id": "r-1"}, settings))
check("run status lists the run and its result", "completed" in runs_out["output"] and "sent the digest" in runs_out["output"], runs_out)

# --- no identity means the tool refuses -------------------------------------
prev = routine_context._current.get()
routine_context._current.set(None)
try:
    refused = asyncio.run(T.run_routine_tool("create_routine", {"name": "x", "task_prompt": "y", "schedule_type": "daily", "time": "09:00"}, settings))
finally:
    routine_context._current.set(prev)
check("create_routine refuses with no signed-in user", "signed-in" in refused["error"], refused)

# --- ambiguous schedules are surfaced ---------------------------------------
fake.override = {"ok": False, "error": "a weekly routine needs at least one weekday", "ambiguous": True}
ambiguous = with_context(lambda: T.run_routine_tool(
    "create_routine", {"name": "x", "task_prompt": "y", "schedule_type": "weekly", "time": "09:00"}, settings))
check("an ambiguous schedule is reported as a question", "ambiguous" in ambiguous["error"].lower(), ambiguous)
fake.override = None

# --- not configured ---------------------------------------------------------
T.RoutineClient = type("NoneFactory", (), {"from_settings": staticmethod(lambda _s: None)})
unconfigured = with_context(lambda: T.run_routine_tool("list_routines", {}, settings))
check("a missing edge config is reported, not guessed", "not configured" in unconfigured["error"], unconfigured)
T.RoutineClient = type("FakeFactory", (), {"from_settings": staticmethod(lambda _s: fake)})

# --- failure classification -------------------------------------------------
def cls(exc):
    return classify_failure(exc)

check("a 429 is retryable as rate_limited", cls(ProviderHTTPError("groq", "m", 429)) == (True, "rate_limited"))
check("a 503 is retryable as provider_unavailable",
      cls(ProviderHTTPError("groq", "m", 503)) == (True, "provider_unavailable"))
check("a 400 is terminal", cls(ProviderHTTPError("groq", "m", 400)) == (False, "provider_rejected"))
check("a gate timeout is retryable as rate_limited", cls(ModelCallGateTimeout("groq", "m", 30.0)) == (True, "rate_limited"))
check("an unavailable provider is retryable", cls(ProviderUnavailable("no key")) == (True, "provider_unavailable"))
check("an unexpected error is terminal", cls(ValueError("boom")) == (False, "ValueError"))

print()
if failures:
    print(f"{len(failures)} routine-tool check(s) failed")
    sys.exit(1)
print("all routine-tool checks pass")
