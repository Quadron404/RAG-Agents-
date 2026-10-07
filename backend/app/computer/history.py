"""The AI-written semantic history: what the model says it just did.

`History.txt` is the run's model memory, and the *model* writes it.  The run's
memory is one sentence per action, written by the agent in its own words, and the
next request carries all of them::

    {"history":"I've clicked the Post button."}

It rides inside the single native tool call, as a required `history` argument::

    {"name":"click","arguments":"{"x":344,"y":107,"history":"I've clicked the Post button."}"}

That transport is not a stylistic choice, it is the only one that works.  Native
tool calling -- Groq's Qwen among them -- returns `content: null` for a tool call
and puts nothing anywhere else; there is no trailing text to read and no prose to
ask for.  A reply that ends with a standalone `{"history": ...}` object is
therefore not a contract any tool-calling endpoint can be held to, and asking for
one means the memory is empty on exactly the providers where the loop works best.
The argument is part of the call the model has to make anyway, so it cannot be
dropped without dropping the action.

The executor's own record is a different thing and stays separate::

    click (344,107) → SUCCESS

which answers *whether it worked*, not *what was being done*, and is only ever
reported by the code that can know.  The two are never merged: a memory of
coordinates teaches the next request nothing it can act on, and a memory the
model wrote about an action the machine refused teaches it that the action
happened.

So this module is the boundary.  It reads the note out of the tool call, refuses
anything it cannot read, and refuses anything that would put the mechanics back
into the memory:

- a note is rejected rather than invented or truncated, so a malformed reply
  costs one turn of history instead of the run's claim to know what it did;
- a note carrying coordinates, a raw tool object, or an executor verdict is
  rejected, because ``click -> x=344,y=107`` is the representation this replaces.

Text extraction is kept as a fallback for a provider that does return trailing
text, but it is never the primary mechanism and never the only one tried.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .tools import HISTORY_ARGUMENT

#: One note, bounded.  A history line is a sentence; anything approaching a
#: paragraph is the model narrating instead of reporting, and it is paid for on
#: every remaining request of the run.  Longer than this is refused rather than
#: clipped: a clipped sentence is not what the model wrote, and silently
#: shortening it is the same class of failure as inventing one.
MAX_HISTORY_NOTE_CHARS = 400

#: The most text the scanner will walk looking for a trailing object.  Only the
#: fallback uses it; the primary source is an already-parsed dict.
MAX_HISTORY_SCAN_CHARS = 20000

#: Mechanics that must never reach the model's memory.  Two shapes, each of which
#: is one of the representations this system exists to stop:
#:
#: - ``344,107`` / ``(344,107)``  a coordinate pair, bare or parenthesised, in
#:   the form the executor writes and the form a model copies out of its own call;
#: - ``x=344``  a coordinate written as a tool argument;
#: - ``success`` / ``failed`` / ``succeeded``  the executor's verdict, which is
#:   not the model's to assert -- the executor reports that separately.
_COORD_PAIR_RE = re.compile(r"\b\d{1,5}\s*,\s*\d{1,5}\b|\b[xy]\s*=\s*-?\d", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(success\w*|succeed\w*|fail\w*)\b", re.IGNORECASE)

#: Raw tool JSON pasted into the note.  A history is a sentence; a note that
#: opens with a brace or quotes an argument key is the model echoing the call
#: back, which is the one thing the call itself already says perfectly well.
_RAW_TOOL_RE = re.compile(r"^\s*[\{\[]|\"(?:name|arguments|parameters)\"\s*:", re.IGNORECASE)


def _validate_note(raw: Any) -> Tuple[str, str]:
    """One candidate sentence, checked.  Returns ``(note, error)``.

    Every rule the memory depends on lives here, so the tool-call path and the
    text fallback cannot disagree about what counts as a history.  Rejections are
    specific, because "history missing" and "history was the executor's log" call
    for different fixes.
    """
    if raw is None:
        return "", "the history argument was not present"
    if not isinstance(raw, str):
        return "", f"the history argument was {type(raw).__name__}, not a string"
    note = " ".join(raw.split())
    if not note:
        return "", "the history argument was empty"
    if len(note) > MAX_HISTORY_NOTE_CHARS:
        return "", (
            f"the history argument was {len(note)} characters; a history line is "
            f"one sentence of at most {MAX_HISTORY_NOTE_CHARS}"
        )
    if _RAW_TOOL_RE.search(note):
        return "", "the history argument was raw tool JSON, not a sentence"
    if _COORD_PAIR_RE.search(note):
        return "", (
            "the history argument contained coordinates; the run's memory is a "
            "semantic description and must not carry the mechanics of the action"
        )
    if _STATUS_RE.search(note):
        return "", (
            "the history argument asserted an executor result; only the executor "
            "reports success or failure"
        )
    return note, ""


def extract_history_argument(args: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """The note carried as an argument of the native tool call.

    This is the primary source and the one that works with `content: null`:
    ``{"x":344,"y":107,"history":"I've clicked the Post button."}``.  Reads the
    argument without mutating the dict -- the runner removes it afterwards, so
    the command is built from the executable fields alone.
    """
    if not isinstance(args, dict):
        return "", "the tool call arguments were not an object"
    if HISTORY_ARGUMENT not in args:
        return "", (
            f"the {HISTORY_ARGUMENT} argument was missing; every native tool "
            "call must carry it"
        )
    return _validate_note(args.get(HISTORY_ARGUMENT))

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
    """Fallback: a `{"history": ...}` object at the end of the assistant text.

    Kept for a provider that does return trailing text, and only as a fallback.
    A native tool-calling endpoint generally returns `content: null`, so this
    cannot be the mechanism the loop depends on.
    """
    if not isinstance(text, str):
        return "", "there was no assistant text to read a history from"

    found: str = ""
    error = "there was no {\"history\": ...} object in the assistant text"
    for value in _candidate_notes(text):
        if HISTORY_ARGUMENT not in value:
            continue
        note, why = _validate_note(value.get(HISTORY_ARGUMENT))
        if note:
            found, error = note, ""
        elif why:
            error = f"in the assistant text: {why}"
    return found, error


def extract_history(
    args: Optional[Dict[str, Any]], reply_text: object = None
) -> Tuple[str, str]:
    """The note for one model reply: tool-call argument first, trailing text second.

    The order is the design, and it is decided by *presence*, not by validity.
    When the argument is there it is the whole answer: if it reads, that is the
    memory, and if it does not, the refusal stands.  Trailing text is consulted
    only when the argument is absent altogether.

    That distinction is deliberate.  Letting a fallback rescue a refused argument
    would mean the rules above are advisory -- the loop could refuse
    ``click -> x=344,y=107`` as mechanics and then store the sentence that
    followed it, so "the memory holds semantics" would depend on which of two
    texts happened to be scanned last.  A model that writes coordinates in the
    argument gets no memory entry for that turn, which is visible and fixable,
    rather than a memory that was quietly laundered through a second channel.

    Returns ``(note, error)``; exactly one is meaningful.  A missing or refused
    note is never invented and never blocks the action: the executor decides
    whether the run may continue, not this module.
    """
    note, error = extract_history_argument(args)

    # Present-and-unusable is answered here and only here.
    if isinstance(args, dict) and HISTORY_ARGUMENT in args:
        return note, error

    text_note, text_error = extract_history_note(reply_text)
    if text_note:
        return text_note, ""

    if text_error:
        # Neither transport carried one.  Report both reasons rather than
        # guessing which is the one to fix.
        return "", f"{error}; {text_error}"
    return "", error


def history_line(note: str) -> str:
    """One stored entry, in the exact form the next request shows.

    Re-serialised through the JSON encoder rather than concatenated, so a note
    containing a quote, a backslash or a newline cannot produce a broken line in
    the next request.
    """
    return json.dumps({HISTORY_ARGUMENT: " ".join(str(note or "").split())}, ensure_ascii=False)


def compact_history(entries, max_lines: int = 0) -> List[str]:
    """The stored notes as the next request should receive them: bounded and deduped.

    Two things a run's memory accumulates and a request must not pay for:

    - *repetition*.  A model that writes the same sentence after every step
      spends one line of every remaining request on a fact the first line
      already carried.  Only *consecutive* repeats collapse, so a control
      pressed, left, and pressed again still reads as two separate moments --
      the repetition being collapsed is the same note written twice in a row,
      which is memory failing rather than memory reporting.
    - *length*.  The newest lines are the useful ones, so the bound keeps them
      and drops from the front.  Applied after the collapse, so a run of
      identical notes costs one line rather than consuming the budget that
      would have held the distinct entries behind it.

    Returns plain sentences; `history_block` is what turns them into the
    `{"history": ...}` lines of the request.
    """
    compact: List[str] = []
    for entry in entries:
        note = " ".join(str(entry or "").split())
        if not note:
            continue
        if compact and compact[-1] == note:
            continue
        compact.append(note)
    if max_lines > 0 and len(compact) > max_lines:
        compact = compact[-max_lines:]
    return compact


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
    "compact_history",
    "extract_history",
    "extract_history_argument",
    "extract_history_note",
    "history_block",
    "history_line",
]