"""Canonical conversation history and the context the ordinary agents see.

The backend already stores every turn in SQLite, but the runner used to rebuild
the model's context by filtering for two roles and dropping everything else --
no tool calls, no tool results, no worker reports -- and then appending the
current user turn a second time.  The model was therefore shown a conversation
it had not actually had, with the newest question duplicated.

This module is the one place that turns the stored transcript into the message
list a model is asked to continue.  Three rules it is built around:

* **The transcript is the source of truth.**  Context is a *view* of stored
  rows, never a second copy.  If it is not in the database it is not in the
  context, and if it is in the database exactly once it appears exactly once.
* **Tool work is part of the conversation.**  A tool call, its result and a
  worker's report are turns like any other; dropping them is what made a resumed
  conversation disagree with its own history.
* **Budget is spent on relevance, not on recency alone.**  The newest turns are
  kept verbatim; older turns are pulled back only when they are lexically
  similar to the current question.  Nothing is included merely because it was
  once read into the process -- there is no source, README or file dump here.
"""
from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Tuple

from ..providers.base import LLMMessage

#: Roughly four characters per token.  Deliberately the same estimate everywhere
#: so the budget means one thing: an *estimate* is enough because the real token
#: count is the provider's to report, and guessing it precisely here would only
#: add a second, wrong number to disagree with the provider's.
_CHARS_PER_TOKEN = 4

_WORD_RE = re.compile(r"[a-z0-9_]+")


def estimate_tokens(text: str) -> int:
    text = text or ""
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def _terms(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


def lexical_score(query_terms: set, text: str) -> float:
    """Jaccard-like overlap between the query and a candidate text.

    Normalised by the candidate's length so a long, rambling turn cannot win the
    budget merely by containing the query words among many others.  Ties are
    broken by the caller keeping original order, which keeps the retrieved turns
    readable in sequence.
    """
    if not query_terms:
        return 0.0
    terms = _terms(text)
    if not terms:
        return 0.0
    overlap = len(query_terms & terms)
    if not overlap:
        return 0.0
    return overlap / float(len(terms) ** 0.5)


#: A worker row's ``agent`` is ``worker:<role>:<title>``; a tool row records the
#: tool name in its JSON blob.  These are read back out so a resumed conversation
#: shows *what* happened, not just that something did.
_WORKER_PREFIX = "worker:"


def message_from_row(row: Dict[str, object]) -> Optional[LLMMessage]:
    """One stored transcript row as a single LLM message.

    Returns ``None`` for rows that carry no model-visible content (an empty
    worker report, for instance) so they cannot occupy a budget slot while
    contributing nothing.
    """
    role = str(row.get("role") or "")
    agent = str(row.get("agent") or "")
    content = str(row.get("content") or "")
    try:
        data = json.loads(row.get("data") or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    if role == "tool":
        name = str(data.get("tool") or data.get("name") or "tool")
        if not content.strip():
            return None
        return LLMMessage("tool", content, tool_call_id=str(data.get("id") or ""), name=name)

    if role == "assistant" and data.get("tool_calls"):
        # A stored assistant turn that requested tools (native function-calling).
        from ..providers.base import ToolCall

        calls = []
        for raw in data.get("tool_calls") or []:
            if not isinstance(raw, dict):
                continue
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    arguments=str(raw.get("arguments") or "{}"),
                )
            )
        if calls:
            return LLMMessage("assistant", content, tool_calls=calls)

    if role in ("user", "assistant", "system"):
        if not content.strip():
            return None
        return LLMMessage(role, content)

    return None


def dedupe_tool_results(messages: List[LLMMessage]) -> List[LLMMessage]:
    """Keep only the newest result for each ``tool_call_id``.

    A retried tool can leave two rows with the same call id.  Showing both makes
    the model believe it received two answers; the later one is the one the run
    actually acted on.  Messages without an id are never touched.
    """
    last_index: Dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.role == "tool" and message.tool_call_id:
            last_index[message.tool_call_id] = index
    out: List[LLMMessage] = []
    for index, message in enumerate(messages):
        if message.role == "tool" and message.tool_call_id:
            if last_index.get(message.tool_call_id) != index:
                continue
        out.append(message)
    return out


def canonical_messages(
    rows: Iterable[Dict[str, object]], *, exclude_id: Optional[int] = None
) -> List[LLMMessage]:
    """Every model-visible turn from the stored rows, in order, exactly once.

    ``exclude_id`` drops the single transcript row for the turn currently being
    produced.  The caller has already stored that user turn (so the transcript is
    durable before the model is asked anything) and the agent runner appends the
    same text itself; excluding it here is what keeps the newest question from
    appearing twice.
    """
    messages: List[LLMMessage] = []
    for row in rows:
        try:
            if exclude_id is not None and int(row.get("id") or 0) == int(exclude_id):
                continue
        except (TypeError, ValueError):
            pass
        message = message_from_row(row)
        if message is not None:
            messages.append(message)
    return dedupe_tool_results(messages)


def assemble_context(
    rows: Iterable[Dict[str, object]],
    *,
    query: str,
    token_budget: int,
    exclude_id: Optional[int] = None,
    summary_text: str = "",
    memory_text: str = "",
    recent_verbatim: int = 12,
) -> List[LLMMessage]:
    """Build the ordinary agent's message list within a token budget.

    The budget is spent in this order, and only on these things:

    1. the standing memory block (small, and the whole point of remembering);
    2. the rolling summary of earlier conversation (the compressed past);
    3. the most recent ``recent_verbatim`` turns, verbatim;
    4. as many remaining older turns as fit, ranked by lexical relevance to
       ``query`` and re-inserted in their original order.

    Nothing is read from disk, the repository or any file; "relevant" means
    similar to the current question, not "whatever was loaded most recently".
    """
    ordered = canonical_messages(rows, exclude_id=exclude_id)

    system_messages: List[LLMMessage] = []
    spent = 0
    if memory_text.strip():
        block = "Long-term memory (confirmed = stated by the user, inferred = deduced):\n" + memory_text.strip()
        system_messages.append(LLMMessage("system", block))
        spent += estimate_tokens(block)
    if summary_text.strip():
        block = "Summary of earlier conversation:\n" + summary_text.strip()
        system_messages.append(LLMMessage("system", block))
        spent += estimate_tokens(block)

    remaining = max(0, int(token_budget) - spent)

    # 4a. Recent turns, verbatim, newest backwards until the budget is reached.
    n = len(ordered)
    keep_recent_from = n
    recent_spent = 0
    for index in range(n - 1, -1, -1):
        if n - index > max(1, recent_verbatim) and recent_spent > 0:
            break
        cost = estimate_tokens(ordered[index].content)
        if recent_spent + cost > remaining and index < n - 1:
            break
        recent_spent += cost
        keep_recent_from = index
    recent_turns = ordered[keep_recent_from:]
    leftover = max(0, remaining - recent_spent)

    # 4b. Older turns, selected by lexical relevance, kept in original order.
    older = ordered[:keep_recent_from]
    query_terms = _terms(query)
    scored = [
        (i, lexical_score(query_terms, m.content))
        for i, m in enumerate(older)
    ]
    scored = [(i, score) for i, score in scored if score > 0.0]
    scored.sort(key=lambda pair: (-pair[1], -pair[0]))
    chosen: Dict[int, float] = {}
    for index, score in scored:
        cost = estimate_tokens(older[index].content)
        if cost > leftover:
            continue
        leftover -= cost
        chosen[index] = score
    retrieved = [older[i] for i in sorted(chosen)]

    return system_messages + retrieved + recent_turns


def render_transcript(rows: Iterable[Dict[str, object]]) -> str:
    """A compact, human-readable rendering of a span, for the summariser.

    Kept plain deliberately: the summariser is asked to compress a conversation,
    so its input is the conversation's *text*, with the speaker and tool name
    prefixed, and no application metadata it would have to be told to ignore.
    """
    lines: List[str] = []
    for row in rows:
        role = str(row.get("role") or "")
        agent = str(row.get("agent") or "")
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            speaker = "User"
        elif role == "tool":
            speaker = f"Tool[{agent or 'tool'}]"
        elif role == "assistant" and agent.startswith(_WORKER_PREFIX):
            speaker = f"Worker[{agent[len(_WORKER_PREFIX):]}]"
        elif role == "assistant" and agent:
            speaker = agent.capitalize()
        elif role == "assistant":
            speaker = "Assistant"
        else:
            speaker = role or "?"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


__all__ = [
    "estimate_tokens",
    "lexical_score",
    "message_from_row",
    "dedupe_tool_results",
    "canonical_messages",
    "assemble_context",
    "render_transcript",
]
