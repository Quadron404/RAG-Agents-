"""The tools the computer-control model may call, and the rules on each one.

Ten tools, and the reason there are exactly ten is that every one of them maps
to exactly one method on ``RemoteComputer`` or to bookkeeping that does not
touch the machine.  A tool the loop cannot perform would have to be either
ignored -- so the model is told it happened -- or performed by a second,
unreviewed code path.  Neither is available here.

Two of the ten are the ones the whole loop is shaped around:

``screenshot``
    The only source of an image.  Nothing attaches one automatically, so the
    model decides whether a turn needs to see the screen or whether the text
    history already answers the question.  That decision *is* the token
    saving: an image costs orders of magnitude more than a line of text, and the
    old loop paid for one on every turn whether or not the model used it.

``history``
    One short text line per state change, written by the model, stored on the
    run, and sent back as the compact history.  It is text because that is the
    only thing that survives: a screenshot in the history is the expensive part
    coming back, which is exactly what this loop exists to stop.

The remaining eight are the machine's existing executors, unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .commands import (
    ALLOWED_TYPES,
    FIRST_TURN_TYPES,
    KEY_ALLOWLIST,
    MAX_QUERY_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_URL_LENGTH,
    MODIFIER_ALLOWLIST,
    Bounds,
    Command,
    normalize_key,
)

#: The complete tool surface, in the order the prompt lists it.  Every name here
#: is dispatched in `_perform_tool`; a name that is in the schemas but not in
#: that dispatch would be advertised and unperformable, which the test suite
#: refuses.
TOOL_NAMES = (
    "screenshot",
    "navigate",
    "search",
    "click",
    "type",
    "key",
    "scroll",
    "done",
    "error",
)

#: Tools that change the remote browser, and therefore require a `history` note
#: before the next action.  `screenshot` changes nothing, which is why asking
#: for one does not oblige the model to describe it afterwards -- it read
#: something, it did not do anything.
STATE_CHANGING_TOOLS = frozenset({"navigate", "search", "click", "type", "key", "scroll"})

#: One line of history, bounded.  Long enough to say what happened and short
#: enough that a dozen of them are still cheaper than one screenshot, which is
#: the comparison the whole design rests on.
MAX_HISTORY_NOTE_LENGTH = 12000


def computer_tools(allowed_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return only the tool schemas the current request needs.

    The full tool catalogue is unnecessary on every turn.  The runner narrows
    it for simple tasks so the model has fewer choices and the provider receives
    fewer recurring input tokens.  No new capability is created here: filtering
    only removes schemas from the request.
    """
    def with_history(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
        props = dict(properties)
        props["history"] = {
            "type": "string",
            "description": (
                "Complete current History.txt in plain text. Rewrite the whole "
                "history on every call. Include all important prior facts plus "
                "this turn. Never include screenshots, base64, or JSON."
            ),
        }
        return {
            "type": "object",
            "properties": props,
            "required": [*required, "history"],
        }

    schemas = [
        {
            "name": "screenshot",
            "description": "Get the current VM screen. Call only when you need to see it.",
            "parameters": with_history({}, []),
        },
        {
            "name": "navigate",
            "description": "Open a URL in the real remote browser.",
            "parameters": with_history({"url": {"type": "string", "description": "http(s) URL"}}, ["url"]),
        },
        {
            "name": "search",
            "description": "Open the browser's search engine for a query.",
            "parameters": with_history({"query": {"type": "string"}}, ["query"]),
        },
        {
            "name": "click",
            "description": "Click at a pixel of the latest screenshot.",
            "parameters": with_history({"x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"]),
        },
        {
            "name": "type",
            "description": "Type text into whatever is focused.",
            "parameters": with_history({"text": {"type": "string"}}, ["text"]),
        },
        {
            "name": "key",
            "description": "Press a key such as ENTER, TAB, ESC or CTRL+L.",
            "parameters": with_history({"key": {"type": "string"}}, ["key"]),
        },
        {
            "name": "scroll",
            "description": "Scroll the page. Positive is down.",
            "parameters": with_history({"delta_y": {"type": "integer"}}, ["delta_y"]),
        },
        {
            "name": "done",
            "description": "Finish: the task is complete.",
            "parameters": with_history({"message": {"type": "string"}}, ["message"]),
        },
        {
            "name": "error",
            "description": "Stop: the task cannot be completed safely.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    ]
    if allowed_names is None:
        return schemas
    allowed = set(allowed_names)
    return [schema for schema in schemas if schema["name"] in allowed]


def parse_arguments(raw: Any) -> Optional[Dict[str, Any]]:
    """The tool call's arguments as a dict, or None when they are not one.

    None and {} are deliberately different answers, because the caller has to
    be able to tell them apart.  A call to a tool that takes no arguments
    arrives as the string "{}", which is a perfectly valid object and the
    commonest reply there is; treating it as a parse failure refuses the one
    call that had nothing to get wrong, and a caller cannot recover the
    difference afterwards because both cases came back as the same empty dict.

    So this returns a dict whenever the arguments are a JSON object -- including
    an empty one -- and None only when they are not an object at all: malformed
    text, a bare array, a number, a string.  A model that gets this wrong gets a
    refusal naming the tool, rather than a traceback in place of its own
    mistake.
    """
    import json

    if isinstance(raw, dict):
        return raw
    # An endpoint that omits the field entirely sends nothing, which for a tool
    # with no parameters means the same thing as "{}", and for a tool with
    # parameters is caught by the argument validation further down.
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    # Valid JSON that is not an object: still unusable as arguments, and
    # reported as such rather than silently becoming no arguments at all.
    return value if isinstance(value, dict) else None


def _finite_number(value: Any) -> Optional[float]:
    """A real, finite number, or None.

    bool is refused explicitly: in Python `True` is an int, so without this a
    model sending `{"x": true}` would be read as a click at (1, 0).
    """
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def tool_to_command(
    name: str,
    args: Dict[str, Any],
    bounds: Optional[Bounds],
) -> Tuple[Optional[Command], str]:
    """Validate one tool call into a command, or explain why it cannot be one.

    The same validation the JSON protocol used, reached from a tool call rather
    than from a parsed object: same allowlist, same URL scheme check, same key
    tables, same coordinate bounds.  Validation is not duplicated per protocol,
    because a second copy of an allowlist is a second set of rules that will
    eventually disagree with the first.
    """
    if name not in TOOL_NAMES:
        return None, f"{name!r} is not a tool; the tools are {', '.join(TOOL_NAMES)}"

    history = args.get("history")
    if not isinstance(history, str):
        return None, f'{name} requires a string "history" containing the complete History.txt'
    history = history.strip()[:MAX_HISTORY_NOTE_LENGTH]

    if name == "screenshot":
        # Carried as a command so it lands in the event log and the trace like
        # every other call, but it has no executor and cannot move anything.
        return Command(type="screenshot", history=history), ""

    if name == "navigate":
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return None, 'navigate requires a non-empty string "url"'
        url = url.strip()
        if len(url) > MAX_URL_LENGTH:
            return None, f"url is longer than {MAX_URL_LENGTH} characters"
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None, f"only http and https urls are allowed, not {parsed.scheme or 'no'} scheme"
        if not parsed.netloc:
            return None, "url has no host"
        return Command(type="navigate", url=url, history=history), ""

    if name == "search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return None, 'search requires a non-empty string "query"'
        query = query.strip()
        if len(query) > MAX_QUERY_LENGTH:
            return None, f"query is longer than {MAX_QUERY_LENGTH} characters"
        return Command(type="search", query=query, history=history), ""

    if name in ("click", "move"):
        x = _finite_number(args.get("x"))
        y = _finite_number(args.get("y"))
        if x is None or y is None:
            return None, f"{name} requires numeric x and y"
        if bounds is not None and not bounds.contains(x, y):
            # Refused against the size of the screenshot the model was shown,
            # not against a configured screen size: a coordinate is only
            # meaningful in the grid it was read from.
            return None, (
                f"{name} at ({int(x)}, {int(y)}) is outside the "
                f"{bounds.width}x{bounds.height} screenshot; take a screenshot "
                f"and use coordinates from it"
            )
        return Command(type=name, x=x, y=y, history=history), ""

    if name == "type":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            return None, 'type requires a non-empty string "text"'
        if "\x00" in text:
            return None, "type text may not contain a null byte"
        if len(text) > MAX_TEXT_LENGTH:
            return None, f"text is longer than {MAX_TEXT_LENGTH} characters"
        return Command(type="type", text=text, history=history), ""

    if name == "key":
        combo, error = normalize_key(args.get("key"))
        if error:
            return None, error
        return Command(type="key", key=combo, history=history), ""

    if name == "scroll":
        delta = _finite_number(args.get("delta_y"))
        if delta is None:
            return None, "scroll requires a numeric delta_y"
        from .commands import MAX_SCROLL_DELTA

        if abs(delta) > MAX_SCROLL_DELTA:
            return None, f"delta_y must be between -{MAX_SCROLL_DELTA} and {MAX_SCROLL_DELTA}"
        return Command(type="scroll", delta_y=int(delta), history=history), ""

    if name == "history":
        note = args.get("note")
        if not isinstance(note, str) or not note.strip():
            return None, 'history requires a non-empty string "note"'
        note = " ".join(note.split())[:MAX_HISTORY_NOTE_LENGTH]
        return Command(type="history", text=note), ""

    # done / error
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return None, f'{name} requires a non-empty string "message"'
    from .commands import MAX_MESSAGE_LENGTH

    message = message.strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        return None, f"message is longer than {MAX_MESSAGE_LENGTH} characters"
    return Command(type=name, message=message, history=history), ""


__all__ = [
    "TOOL_NAMES",
    "STATE_CHANGING_TOOLS",
    "MAX_HISTORY_NOTE_LENGTH",
    "computer_tools",
    "parse_arguments",
    "tool_to_command",
    "ALLOWED_TYPES",
    "FIRST_TURN_TYPES",
    "KEY_ALLOWLIST",
    "MODIFIER_ALLOWLIST",
]
