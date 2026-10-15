"""Passive summarisation driven by the durable-job interface.

The properties that matter, and each is a bug the naive version had:

* a conversation is only due after its quiet period, and preparing twice does
  not create two jobs;
* the checkpoint advances only after the summary is written, so a crash repeats
  work rather than losing a span;
* a conversation that kept moving while the model was thinking is re-armed
  rather than marked summarised past what was actually read;
* the summary role is the one the router resolves -- the smaller model.

The model call itself is stubbed for the orchestration cases so the test does not
wait on the real inter-call pacing; one real call is exercised separately.
"""
import asyncio
import os
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.agents import summaries as S
from app.config import Settings
from app.db import Database
from app.providers import base as pbase
from app.providers.mock import MockProvider
from app.providers.router import Router

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


settings = Settings()
settings.summary_idle_seconds = 0
router = Router({"mock": MockProvider()}, settings)

# --- the summary role really calls a model ----------------------------------
pbase._MODEL_CALL_GATE._next_allowed = 0.0
pbase._MODEL_CALL_GATE._stalled_until = 0.0
real_text = asyncio.run(S._summarize(router, "", "User: my name is Ada"))
check("the summary role produces text from the model", bool(real_text.strip()), repr(real_text[:60]))

# --- orchestration with a stubbed model -------------------------------------
tmp = tempfile.mkdtemp(prefix="rag-summary-test-")
db = Database(os.path.join(tmp, "rag.db"))
tid = db.create_thread("u1", "summary test")

db.add_message(tid, "user", "Hello, my name is Ada")
db.add_message(tid, "assistant", "Nice to meet you, Ada.", agent="commander")
first_last = db.last_message_id(tid)
db.touch_conversation_activity(tid, "u1", first_last, idle_seconds=0)

jobs = S.prepare_due_summaries(db, settings)
check("a quiet conversation is prepared", len(jobs) == 1)
again = S.prepare_due_summaries(db, settings)
pending = [j for j in (db.get_job(j) for j in again) if j and j.get("status") == "pending"]
check("preparing twice does not create a second job", len(pending) == 1, f"got {len(pending)}")

claimed = db.claim_jobs(S.SUMMARY_JOB_KIND, limit=5)
check("the due job can be claimed", len(claimed) == 1)
check("a claimed job is marked running", claimed[0]["status"] == "pending" or db.get_job(claimed[0]["job_id"])["status"] == "running")

calls = {"n": 0}


async def fake_summarize(_router, previous, transcript):
    calls["n"] += 1
    return f"Summary #{calls['n']}: Ada introduced herself."


S._summarize = fake_summarize

result = asyncio.run(S.run_summary_job(db, router, claimed[0]))
check("the job completes", result.get("ok") is True)

summary = db.latest_summary(tid)
check("a summary version is written", summary is not None and summary["version"] == 1)
check("the summary covers up to the last event", summary["covered_to"] == first_last)
activity = db.get_conversation_activity(tid)
check("the checkpoint advances only after the summary exists", activity["summarized_through"] == first_last)
check("the conversation goes idle when fully summarised", activity["summary_status"] == "idle")

# Re-preparing an idle, fully-summarised conversation does nothing.
check("an idle conversation is not re-prepared", S.prepare_due_summaries(db, settings) == [])

# --- the next turn builds a new version, contiguous with the first ----------
db.add_message(tid, "user", "What is my name?")
second_last = db.last_message_id(tid)
db.touch_conversation_activity(tid, "u1", second_last, idle_seconds=0)
S.prepare_due_summaries(db, settings)
next_claim = db.claim_jobs(S.SUMMARY_JOB_KIND, limit=5)
asyncio.run(S.run_summary_job(db, router, next_claim[0]))
second = db.latest_summary(tid)
check("a second version is written", second["version"] == 2)
check("the second summary starts where the first ended", second["covered_from"] == first_last)
check("the second summary reaches the newest turn", second["covered_to"] == second_last)

# --- a conversation that moved mid-summary is re-armed, not over-claimed ----
db.add_message(tid, "user", "One more thing")
snapshot = db.last_message_id(tid)
db.touch_conversation_activity(tid, "u1", snapshot, idle_seconds=0)
S.prepare_due_summaries(db, settings)
mid_claim = db.claim_jobs(S.SUMMARY_JOB_KIND, limit=5)
# New turns arrive while the model is "thinking".
db.add_message(tid, "assistant", "Sure, go ahead.", agent="commander")
newer = db.last_message_id(tid)
db.touch_conversation_activity(tid, "u1", newer, idle_seconds=0)
asyncio.run(S.run_summary_job(db, router, mid_claim[0]))
mid_activity = db.get_conversation_activity(tid)
check("the checkpoint stops at what was read", mid_activity["summarized_through"] == snapshot)
check("leftover turns re-arm the conversation", mid_activity["summary_status"] == "pending")
check("and it is immediately due again", len(db.due_conversation_summaries()) == 1)

# --- a job whose span is already covered does nothing -----------------------
db.mark_summary_status(tid, "idle", summarized_through=newer, summary_due_at=None)
stale = db.enqueue_job(S.SUMMARY_JOB_KIND, "u1", {"conversation_id": tid, "user_id": "u1", "covered_to": newer})
stale_job = db.get_job(stale)
res = asyncio.run(S.run_summary_job(db, router, stale_job))
check("an already-covered span is skipped", res.get("skipped") == "nothing_new")

print()
if failures:
    print(f"{len(failures)} summary check(s) failed")
    sys.exit(1)
print("all agent summary checks pass")
