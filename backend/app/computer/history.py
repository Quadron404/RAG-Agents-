"""The AI-written semantic history: what the model says it just did.

`History.txt` is the run's model memory, and the *model* writes it.  Every
computer-control reply ends with exactly one JSON object::

    {"history":"I've clicked the Post button."}

and that object -- the model's own sentence, not a translation of the executor's
log -- is what the next request carries.  The executor's fact line
``click (499,375) → SUCCESS`` stays where it belongs, on the trace, because it
answers a different question: not *what* the agent did but *whether it worked*.

So this module is the boundary between the two.  It extracts the model-written
object from whatever the provider returned, refuses anything it cannot read, and
refuses anything that would put mechanics back into the memory:

- a note is rejected rather than invented, so a malformed reply costs one turn of
  history instead of the run's claim to know what it did;
- a note carrying coordinates, a raw tool name with its arguments, or an
  executor status is rejected, because ``{"history":"click (499,375) → SUCCESS"}``
  is precisely the representation this replaces.

Extraction is deliberately structural rather than textual: the JSON object is
located by balanced scanning that understands strings and escapes, so it does not
matter whether the model put it on its own line, after prose, inside a markdown
fence, or split across streamed chunks that the caller has already joined.  The
tool call's own arguments never reach here -- they arrive on a separate
`ToolCall` -- so the parser cannot mistake ``{"x":499,"y":375}`` for a history.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

#: One note, bounded.  A history line is a sentence; anything approaching a
#: paragraph is the model narrating instead of reporting, and it is paid for on
#: every remaining request of the run.
MAX_HISTORY_NOTE_CHARS = 400

#: The most text the scanner will walk looking for the object.  The contract puts
#: the history at the end of a reply whose ceiling is a few hundred tokens, so
#: this only exists so a pathological reply cannot turn extraction into the most
#: expensive part of the turn.
MAX_HISTORY_SCAN_CHARS = 20000

#: Mechanics that must never reach the model's memory.  Two shapes, each of
#: which is one of the representations this system exists to stop:
#:
#: - ``499,375`` / ``(499,375)``  a coordinate pair, bare or parenthesised, in
#:   the form the executor writes and the form a model copies from its own call;
#: - ``x=54``  a coordinate written as a tool argument;
#: - ``SUCCESS`` / ``FAILED`` the executor's verdict, which is not the model's to
#:   assert -- the executor reports that separately and truthfully.
_COORD_PAIR_RE = re.compile(r"\b\d{1,5}\s*,\s*\d{1,5}\b|\b[xy]\s*=\s*-?\d", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(SUCCESS|FAILED|FAILURE)\b")


def _balanced_object(text: str, start: int) -> Optional[str]:
    """The complete JSON object beginning at ``text[start]``, or None.

    Walks the braces while tracking whether it is inside a string, so a `}` in
    ``"I've clicked }"`` does not end the object early and an escaped quote does
    not open one.  ``None`` means the object is never closed, which is the normal
    case for every `{` that is merely prose.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _candidate_notes(text: str):
    """Every complete top-level object in ``text``, last one last.

    Ordered so the caller can prefer the final object: the contract puts the
    history at the end of the reply, and a model that mentioned an example
    earlier in the same reply should not have that example become the run's
    memory.
    """
    scanned = text[-MAX_HISTORY_SCAN_CHARS:]
    offset = len(text) - len(scanned)
    index = 0
    while True:
        start = scanned.find("{", index)
        if start < 0:
            return
        raw = _balanced_object(scanned, start)
        if raw is None:
            index = start + 1
            continue
        index = start + 1
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def extract_history_note(text: object) -> Tuple[str, str]:
    """The note the model wrote for the action it just issued.

    Returns ``(note, error)``, exactly one of which is meaningful.  ``note`` is
    the model's sentence with whitespace collapsed and length bounded, ready to
    be stored; ``error`` says why there is no note, and is for the trace only --
    a missing note is never invented and never refuses the action, because the
    executor is what decides whether the run may continue.
    """
    if not isinstance(text, str):
        return "", "the model response was not text"

    found: Optional[str] = None
    malformed = False
    for value in _candidate_notes(text):
        if "history" not in value:
            continue
        note = value.get("history")
        if not isinstance(note, str) or not note.strip():
            # Present but unusable.  Keep looking -- a later object may be the
            # real one -- but remember that something was wrong.
            malformed = True
            continue
        found = " ".join(note.split())[:MAX_HISTORY_NOTE_CHARS]
        malformed = False

    if found is None:
        if malformed:
            return "", '{"history": ...} was present but its value was not a non-empty string'
        return "", "no {\"history\": ...} JSON object at the end of the model response"

    if _COORD_PAIR_RE.search(found):
        return "", (
            "the history note contained coordinates; the run's memory is a "
            "semantic description and must not carry the mechanics of the action"
        )
    if _STATUS_RE.search(found):
        return "", (
            "the history note asserted an executor result; only the executor "
            "reports success or failure"
        )
    return found, ""


def history_line(note: str) -> str:
    """One stored entry, in the exact form the next request shows.

    Re-serialised through the JSON encoder rather than concatenated, so a note
    containing a quote, a backslash or a newline cannot produce a broken line in
    the next request.
    """
    return json.dumps({"history": " ".join(str(note or "").split())}, ensure_ascii=False)


def history_block(entries, limit_chars: int = 12000) -> str:
    """The stored entries as one block, newest last, bounded.

    Bounded from the *front*: the oldest line is the one dropped when the block
    runs over, because the most recent actions are the ones a model needs to know
    about, and a truncated tail would leave the block ending mid-JSON.
    """
    lines = [history_line(entry) for entry in entries if str(entry or "").strip()]
    if not lines:
        return ""
    block = "\n".join(lines)
    if len(block) <= limit_chars:
        return block
    kept: List[str] = []
    total = 0
    for line in reversed(lines):
        size = len(line) + 1
        if total + size > limit_chars:
            break
        kept.append(line)
        total += size
    return "\n".join(reversed(kept))


__all__ = [
    "MAX_HISTORY_NOTE_CHARS",
    "extract_history_note",
    "history_block",
    "history_line",
]