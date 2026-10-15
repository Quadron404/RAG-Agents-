"""The routine dispatch endpoint: execute a due routine exactly once.

The Worker's scheduler calls ``POST /internal/routines/execute``.  The
properties that matter here are the ones a dispatch seam must not get wrong:

* the shared internal token is required;
* the same run dispatched twice is executed once -- the second call returns the
  first result rather than running the task again;
* a run already in flight is reported as retryable, not started a second time;
* a rate limit or outage is reported retryable and the job is *retained* (re-
  armed), so the edge delays it rather than losing it;
* a terminal failure is reported as such and the job is failed.

The task runner is stubbed for these checks: they are about the endpoint's
idempotency and retry glue, not about a model.  The task runner itself, and the
edge's recurrence, are covered by ``test_routines_tools`` and the Worker suite.
"""
import os
import sys
import time

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import tests.support as support  # noqa: E402  (configures DATA_DIR before app.main)

import app.main as main_module  # noqa: E402
from app.providers.errors import ProviderHTTPError  # noqa: E402

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


client = support.raw_client()
db = support.db()
calls = []
behavior = {"return": {"ok": True, "result": "sent the digest"}, "exc": None}


async def fake_run(_router, _settings, _executor, task):
    calls.append(task)
    if behavior["exc"] is not None:
        raise behavior["exc"]
    return behavior["return"]


main_module.agent_routines.run_routine_task = fake_run


def post(idempotency_key, **extra):
    body = {"routine_id": "r-1", "routine_run_id": f"run-{idempotency_key}",
            "idempotency_key": idempotency_key, "owner_id": "u1", "task_prompt": "do it"}
    body.update(extra)
    return client.post("/internal/routines/execute", json=body)


# --- the token is required --------------------------------------------------
no_token = support.raw_client()
no_token.headers.clear()
response = no_token.post("/internal/routines/execute", json={"idempotency_key": "x"})
check("a request without the internal token is refused", response.status_code == 401, response.status_code)


# --- one run, executed once -------------------------------------------------
behavior["return"] = {"ok": True, "result": "sent the digest"}
first = post("k-dedup").json()
check("a due routine runs and returns the real result", first.get("result") == "sent the digest", first)
check("the task ran once", len(calls) == 1, calls)

second = post("k-dedup").json()
check("a re-dispatch is deduplicated", second.get("deduplicated") is True and second.get("result") == "sent the digest", second)
check("the task did not run a second time", len(calls) == 1, calls)


# --- a run already in flight ------------------------------------------------
in_flight = "routine.run:k-inflight"
db.enqueue_job("routine.run", "u1", {}, job_id=in_flight)
db.start_job(in_flight, lease_seconds=600)
before = len(calls)
inflight = post("k-inflight").json()
check("a run already in flight is reported retryable", inflight.get("error_code") == "in_progress", inflight)
check("an in-flight run is not executed twice", len(calls) == before, calls)


# --- a rate limit is retained, not failed -----------------------------------
behavior["return"] = {}
behavior["exc"] = ProviderHTTPError("groq", "gpt-oss", 429)
limited = post("k-429").json()
check("a rate limit is retryable", limited.get("retryable") is True and limited.get("error_code") == "rate_limited", limited)
job = db.get_job("routine.run:k-429")
check("a rate-limited job is retained and re-armed",
      job and job.get("status") == "pending" and float(job.get("available_at") or 0) > time.time(), job)


# --- an outage is retained too ----------------------------------------------
behavior["exc"] = ProviderHTTPError("groq", "gpt-oss", 503)
outage = post("k-503").json()
check("a 5xx is retryable as provider_unavailable",
      outage.get("retryable") is True and outage.get("error_code") == "provider_unavailable", outage)


# --- a terminal failure is failed -------------------------------------------
behavior["exc"] = ValueError("bad task")
terminal = post("k-bad").json()
check("a terminal failure is not retryable", terminal.get("retryable") is False, terminal)
bad_job = db.get_job("routine.run:k-bad")
check("a terminal failure fails the job", bad_job and bad_job.get("status") == "failed", bad_job)


# --- an empty result is terminal --------------------------------------------
behavior["exc"] = None
behavior["return"] = {"ok": False, "error_code": "empty_result"}
empty = post("k-empty").json()
check("an empty result is terminal", empty.get("ok") is False and empty.get("error_code") == "empty_result", empty)


# --- the durable-job primitives behave --------------------------------------
db.enqueue_job("routine.run", "u1", {}, job_id="routine.run:k-prim")
check("the first lease of a job wins", db.start_job("routine.run:k-prim", lease_seconds=600) is True)
check("a second lease under a live lease loses", db.start_job("routine.run:k-prim", lease_seconds=600) is False)

db.enqueue_job("routine.run", "u1", {}, job_id="routine.run:k-expire")
db.start_job("routine.run:k-expire", lease_seconds=0)  # a lease that is already over
check("an expired lease can be reclaimed", db.start_job("routine.run:k-expire", lease_seconds=600) is True)

print()
if failures:
    print(f"{len(failures)} routine-execute check(s) failed")
    sys.exit(1)
print("all routine-execute checks pass")
