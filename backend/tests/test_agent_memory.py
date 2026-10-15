"""Long-term memory: provenance, isolation, and refusing secrets.

A remembered value is only useful if the model knows whether the user stated it
or the assistant guessed it, so provenance is mandatory.  Memory is per-user and
must never cross accounts.  And a store that is replayed into every future
prompt is the worst place for a credential, so a secret-looking value is refused
rather than written.
"""
import os
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.agents import memory as mem
from app.db import Database

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


tmp = tempfile.mkdtemp(prefix="rag-memory-test-")
db = Database(os.path.join(tmp, "rag.db"))

stored_user = mem.remember(db, "u1", "city", "Paris", source=mem.CONFIRMED, confidence=1.0)
stored_inferred = mem.remember(db, "u1", "favorite_color", "blue", source=mem.INFERRED, confidence=0.4)
check("a user-stated fact is stored", stored_user)
check("an inferred fact is stored", stored_inferred)

facts = mem.facts_for(db, "u1")
by_key = {f["key"]: f for f in facts}
check("provenance is preserved for a confirmed fact", by_key["city"]["source"] == "confirmed")
check("provenance is preserved for an inferred fact", by_key["favorite_color"]["source"] == "inferred")
check("confirmed facts are listed before inferred", [f["key"] for f in facts][0] == "city")

block = mem.render_facts(facts)
check("the rendered block separates confirmed from inferred", "Confirmed:" in block and "Inferred:" in block)

# --- isolation --------------------------------------------------------------
check("another user sees nothing", mem.facts_for(db, "u2") == [])
mem.remember(db, "u2", "city", "Berlin", source=mem.CONFIRMED)
check("each user's fact stands alone", mem.facts_for(db, "u1")[0]["value"] == "Paris")
check("the second user's fact is its own", mem.facts_for(db, "u2")[0]["value"] == "Berlin")

# --- secrets are refused ----------------------------------------------------
check("a password key is refused", mem.remember(db, "u1", "password", "hunter2") is False)
check("an api key value is refused", mem.remember(db, "u1", "note", "sk-abcdef0123456789ABCDEF") is False)
check("a bearer-style value is refused", mem.remember(db, "u1", "header", "Authorization: Bearer abcDEF123456") is False)
check("nothing secret was written", all(f["key"] not in ("password", "note", "header") for f in mem.facts_for(db, "u1")))
check("empty values are refused", mem.remember(db, "u1", "blank", "   ") is False)

# --- update / forget --------------------------------------------------------
mem.remember(db, "u1", "city", "Lyon", source=mem.CONFIRMED)
check("a repeated key updates in place", mem.facts_for(db, "u1")[0]["value"] == "Lyon")
check("forget removes the fact", mem.forget(db, "u1", "city") is True)
check("forget of a missing fact reports false", mem.forget(db, "u1", "city") is False)

print()
if failures:
    print(f"{len(failures)} memory check(s) failed")
    sys.exit(1)
print("all agent memory checks pass")
