"""The canonical transcript and the context the model is shown.

Three properties decide whether a resumed conversation is honest:

1. every turn is stored once, with a stable id, and the newest user turn is not
   fed back to the model twice;
2. tool calls, tool results and worker reports are part of the transcript, not
   discarded when context is rebuilt;
3. what the model sees fits the token budget, spending it on the most relevant
   older turns rather than on whatever happened to be loaded.

A failure here is the difference between an assistant that remembers its own
work and one that contradicts it.
"""
import os
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.agents.conversation import (
    assemble_context,
    canonical_messages,
    dedupe_tool_results,
    estimate_tokens,
    message_from_row,
)
from app.db import Database

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


tmp = tempfile.mkdtemp(prefix="rag-history-test-")
db = Database(os.path.join(tmp, "rag.db"))
tid = db.create_thread("u1", "history test")

id1 = db.add_message(tid, "user", "What is the weather in Paris?")
id2 = db.add_message(tid, "assistant", "Let me check.", agent="commander")
id3 = db.add_message(tid, "tool", "Sunny, 21C", agent="web_search", data={"tool": "web_search", "id": "t-1"})
id4 = db.add_message(tid, "assistant", "It is sunny and 21C.", agent="worker:research:Weather")
id5 = db.add_message(tid, "user", "Tell me about the history of quantum computing")

check("ids are assigned and increase", [id1, id2, id3, id4, id5] == sorted([id1, id2, id3, id4, id5]) and id1 > 0)
check("last_message_id is the newest row", db.last_message_id(tid) == id5)

rows = db.list_messages(tid, tail=200)

# --- canonical mapping ------------------------------------------------------
tool_msg = message_from_row({"role": "tool", "content": "x", "agent": "web_search", "data": '{"tool": "web_search", "id": "t-1"}'})
check("a tool row maps to a tool message with its name", tool_msg is not None and tool_msg.role == "tool" and tool_msg.name == "web_search")

worker_msg = message_from_row({"role": "assistant", "content": "done", "agent": "worker:research:Weather", "data": "{}"})
check("a worker row maps to an assistant message", worker_msg is not None and worker_msg.role == "assistant")

all_msgs = canonical_messages(rows)
check("all five turns survive into context", len(all_msgs) == 5, f"got {len(all_msgs)}")
check("tool work is present in context", any(m.role == "tool" for m in all_msgs))
check("worker report is present in context", any(
    m.role == "assistant" and m.content == "It is sunny and 21C." for m in all_msgs
))

excluded = canonical_messages(rows, exclude_id=id5)
check("excluding the current turn drops exactly one row", len(excluded) == 4)
check(
    "the current user turn is not duplicated",
    not any(m.content == "Tell me about the history of quantum computing" for m in excluded),
)

# --- tool-result dedupe -----------------------------------------------------
dup_rows = [
    {"id": 10, "role": "tool", "content": "stale", "agent": "web_search", "data": '{"tool": "web_search", "id": "t-9"}'},
    {"id": 11, "role": "tool", "content": "fresh", "agent": "web_search", "data": '{"tool": "web_search", "id": "t-9"}'},
]
deduped = dedupe_tool_results(canonical_messages(dup_rows))
check("a retried tool keeps only its newest result", [m.content for m in deduped] == ["fresh"])

# --- context assembly -------------------------------------------------------
ctx = assemble_context(
    rows,
    query="weather Paris temperature",
    token_budget=3000,
    exclude_id=id5,
    summary_text="Earlier the user greeted the assistant.",
    memory_text="Confirmed:\n- city = Paris",
    recent_verbatim=1,
)
check("memory is the first system message", ctx[0].role == "system" and "city = Paris" in ctx[0].content)
check("summary is included", any("Earlier the user greeted" in m.content for m in ctx))
check("the excluded current turn is absent", not any("history of quantum computing" in m.content for m in ctx))
check("lexical retrieval pulls back the relevant older turn", any("weather in Paris" in m.content for m in ctx))

spent = sum(estimate_tokens(m.content) for m in ctx)
check("context fits the token budget", spent <= 3000, f"spent {spent}")

# The newest turn is always kept, even when that alone exceeds the budget.
tiny = assemble_context(rows, query="x", token_budget=1, exclude_id=id5, recent_verbatim=1)
check("the newest turn is never dropped", len(tiny) >= 1 and tiny[-1].role == "assistant")

# --- span helpers -----------------------------------------------------------
span = db.messages_between(tid, after_id=id2, through_id=id4)
check("messages_between returns (after, through]", [r["id"] for r in span] == [id3, id4])
since = db.list_messages_since(tid, after_id=id3)
check("list_messages_since returns only newer rows", [r["id"] for r in since] == [id4, id5])

print()
if failures:
    print(f"{len(failures)} history check(s) failed")
    sys.exit(1)
print("all agent history checks pass")
