"""The browser-facing routines routes.

These are the routes the UI reads and manages routines through.  They add no new
authority -- the edge still owns every routine -- so what has to be proven here
is the *gateway* around them:

* a caller with no session is refused, exactly like every other protected route;
* the owner the edge is asked about is the signed-in user's Google subject, never
  something the client supplied -- that is what stops one signed-in user from
  reading another's schedule;
* when the deployment has no routines service, the answer is an explicit
  ``available: false`` with a reason, not an empty list that reads as "none";
* a real edge answer is passed through unchanged, and an edge error or an
  unreachable edge surfaces as an error rather than being swallowed.

The edge client is stubbed (``RoutineClient`` is replaced), because the edge's
own recurrence and run ledger are proven in the Worker suite; this module proves
the seam between the browser and that client.
"""
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import tests.support as support  # noqa: E402  (configures DATA_DIR before app.main)

import app.main as main_module  # noqa: E402

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


# --- a stubbed edge ---------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.calls = []
        self.routine = {
            "routine_id": "r-1",
            "owner_sub": "google-sub-1",
            "name": "Inbox digest",
            "task_prompt": "Summarise the inbox",
            "schedule_type": "daily",
            "timezone": "UTC",
            "schedule_text": "every day at 09:00",
            "enabled": 1,
            "next_run_at": 1_700_000_000_000,
            "last_run_status": "",
        }
        #: When set, every call answers with this {status, body} verbatim.
        self.override = None

    def _ok(self, body):
        return {"status": 200, "body": body}

    async def list_for(self, owner_sub):
        self.calls.append(("list", owner_sub))
        if self.override is not None:
            return self.override
        return self._ok({"ok": True, "routines": [self.routine]})

    async def runs(self, routine_id, owner_sub, limit=20):
        self.calls.append(("runs", routine_id, owner_sub, limit))
        if self.override is not None:
            return self.override
        return self._ok({"ok": True, "runs": [{"routine_run_id": "run-1", "status": "completed"}]})

    async def pause(self, routine_id, owner_sub):
        self.calls.append(("pause", routine_id, owner_sub))
        if self.override is not None:
            return self.override
        return self._ok({"ok": True, "routine": {**self.routine, "enabled": 0}})

    async def resume(self, routine_id, owner_sub):
        self.calls.append(("resume", routine_id, owner_sub))
        if self.override is not None:
            return self.override
        return self._ok({"ok": True, "routine": {**self.routine, "enabled": 1}})

    async def delete(self, routine_id, owner_sub):
        self.calls.append(("delete", routine_id, owner_sub))
        if self.override is not None:
            return self.override
        return self._ok({"ok": True})


edge = FakeClient()


class ConfiguredFactory:
    @staticmethod
    def from_settings(_settings):
        return edge


class UnconfiguredFactory:
    @staticmethod
    def from_settings(_settings):
        return None


main_module.RoutineClient = ConfiguredFactory

# --- a signed-in user with a known Google subject ---------------------------
user = support.sign_in(google_sub="google-sub-1", email="alice@example.com")
client, _session = support.make_client(user["id"])


# --- authentication is required ---------------------------------------------
anonymous = support.raw_client()
anonymous.cookies.clear()
anonymous.headers["x-rag-csrf"] = "whatever"
unauthenticated = anonymous.get("/routines")
check("GET /routines without a session is refused", unauthenticated.status_code == 401, unauthenticated.status_code)


# --- the edge is asked about the session's subject, not the client's ---------
response = client.get("/routines?owner_sub=attacker")
body = response.json()
check("GET /routines succeeds for the signed-in user", response.status_code == 200 and body.get("ok"), body)
check("GET /routines reports the service as available", body.get("available") is True, body)
check("GET /routines returns the edge's routines", len(body.get("routines") or []) == 1, body)
check(
    "GET /routines asks the edge about the session's google_sub, ignoring the query",
    edge.calls[-1] == ("list", "google-sub-1"),
    edge.calls[-1],
)


# --- run history ------------------------------------------------------------
runs = client.get("/routines/r-1/runs")
runs_body = runs.json()
check("GET /routines/{id}/runs succeeds", runs.status_code == 200 and runs_body.get("ok"), runs_body)
check("GET /routines/{id}/runs returns the runs", runs_body.get("runs")[0]["routine_run_id"] == "run-1", runs_body)
check(
    "GET run history is scoped to the session's subject",
    edge.calls[-1] == ("runs", "r-1", "google-sub-1", 20),
    edge.calls[-1],
)


# --- lifecycle --------------------------------------------------------------
paused = client.post("/routines/r-1/pause")
check("POST pause succeeds", paused.status_code == 200 and paused.json().get("ok"), paused.json())
check("POST pause carries the routine id and subject", edge.calls[-1] == ("pause", "r-1", "google-sub-1"), edge.calls[-1])

resumed = client.post("/routines/r-1/resume")
check("POST resume succeeds", resumed.status_code == 200 and resumed.json().get("ok"), resumed.json())
check("POST resume carries the routine id and subject", edge.calls[-1] == ("resume", "r-1", "google-sub-1"), edge.calls[-1])

deleted = client.delete("/routines/r-1")
check("DELETE succeeds", deleted.status_code == 200 and deleted.json().get("ok"), deleted.json())
check("DELETE carries the routine id and subject", edge.calls[-1] == ("delete", "r-1", "google-sub-1"), edge.calls[-1])


# --- an edge that refuses is reported, not hidden ---------------------------
edge.override = {"status": 404, "body": {"ok": False, "error": "no such routine"}}
refused = client.get("/routines")
check("an edge error is surfaced with its message", refused.json().get("ok") is False and "no such routine" in refused.json().get("error", ""), refused.json())
check("an edge error still reports the service as available", refused.json().get("available") is True, refused.json())
edge.override = None

# --- an unreachable edge is an error, not an empty schedule -----------------
edge.override = {"status": 0, "body": {"ok": False, "error": "edge unreachable: boom"}}
unreachable = client.get("/routines")
check(
    "an unreachable edge is surfaced as an error",
    unreachable.json().get("ok") is False and "unreachable" in unreachable.json().get("error", ""),
    unreachable.json(),
)
check("an unreachable edge does not look like 'no routines'", unreachable.json().get("routines") == [], unreachable.json())
edge.override = None

# --- not configured is said plainly, not shown as an empty list -------------
main_module.RoutineClient = UnconfiguredFactory
unconfigured = client.get("/routines")
check(
    "with no routines service the answer is explicitly unavailable",
    unconfigured.json().get("available") is False and "not configured" in unconfigured.json().get("error", ""),
    unconfigured.json(),
)
main_module.RoutineClient = ConfiguredFactory


print()
if failures:
    print(f"{len(failures)} routines-api check(s) failed")
    sys.exit(1)
print("all routines-api checks pass")
