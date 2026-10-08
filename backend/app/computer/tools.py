"""The tools the computer-control model may call, and the rules on each one.

Ten tools, and the reason there are exactly ten is that every one of them maps
to exactly one method on ``RemoteComputer`` or to bookkeeping that does not
touch the machine.  A tool the loop cannot perform would have to be either
ignored -- so the model is told it happened -- or performed by a second,
unreviewed code path.  Neither is available here.

None of the ten is a history tool, and there is no eleventh waiting to become
one.  The run's memory is the ``history`` **argument** every one of these ten
carries, read in ``history.py`` and carried on the next request by the runner.
Making it a *call* would break the loop's central rule -- one executable action
per request -- and would make the memory a thing the executor records rather
than a thing the model writes, which is precisely the distinction this design
turns on.

The argument is required on all ten, including the four that change nothing.  A
memory read from the assistant's prose cannot work here at all: these endpoints
answer a tool call with ``content: null`` and no trailing text, so a note asked
for after the call was asked for from a place that does not exist.  Inside the
call it is part of the action the model had to take anyway, so it cannot be
dropped without dropping the action.

One of the ten is the one the loop is shaped around:

``screenshot``
    The only source of an image.  Nothing attaches one automatically, so the
    model decides whether a turn needs to see the screen or whether the text
    history already answers the question.  That decision *is* the token
    saving: an image costs orders of magnitude more than a line of text, and the
    old loop paid for one on every turn whether or not the model used it.

The remaining nine are the machine's existing executors, unchanged.
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
from .next_step import NEXT_STEP_ARGUMENT, next_step_schema
from .hit_target import clean_target
from .ui_map import ELEMENT_ID_RE

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
    "stop",
    "error",
)

#: Tools that change the remote browser.  `screenshot` changes nothing, which is
#: why asking for one is still an action the model describes in its own words --
#: it read something, it did not move anything.
STATE_CHANGING_TOOLS = frozenset({"navigate", "search", "click", "type", "key", "scroll"})

#: The name of the argument every tool carries.  Not a tool: it is metadata
#: inside a tool call, and there is no `history()` in `TOOL_NAMES` and never will
#: be -- a second call would break the one-call-per-reply rule and would turn the
#: run's memory into something the executor records rather than something the
#: model writes.
HISTORY_ARGUMENT = "history"

#: Why it is required, in the words the model reads.  This description is sent
#: on every request of every run, ten times over, so it is also the recurring place
#: where the semantic rule is enforced: what the sentence may say, and what it may
#: never say.  The negative half matters most, because the executor's own log is
#: exactly what a model copies when left to guess.
#:
#: Kept to one sentence on purpose.  The rules that are *not* here live in the
#: system prompt instead -- how the sentence is used and when to move on -- because
#: a rule repeated in all ten schemas is paid for ten times to say one thing, and
#: this block goes out on every request of every turn.
HISTORY_DESCRIPTION = (
    "Required. One short sentence about the action you are issuing. No "
    "coordinates, arguments, or success/failure claims."
)

#: The other metadata argument every tool carries, and the reason it exists.
#:
#: `history` says what the model just did; this says what it means to do next.
#: Both are arguments of the one call the model had to make anyway, because a
#: native tool call is the only channel these endpoints fill -- a reply that ends
#: a tool call has no prose to read either of them out of.  See `next_step.py`.
#:
#: Short for the same reason as `HISTORY_DESCRIPTION` and because the rules it
#: used to carry are all still in force somewhere cheaper: the system prompt says
#: to continue from the plan and to revise it when the state disagrees, and it is
#: the one place that also says a terminal call plans `"tool": "none"`.  The two
#: prohibitions kept here are the two the validator enforces on the plan itself,
#: and a model that does not know them loses the plan rather than the action --
#: so two clauses here are cheaper than ten refused replies.
NEXT_STEP_DESCRIPTION = (
    "Required. Plan the next step after this tool finishes: the next tool, what "
    "it should accomplish, and what to verify first. Guidance only; never already "
    "done; no coordinates."
)


def _object_schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    """A tool's parameters, with `history` and `next_step` added and required.

    Every tool takes both, including the four that change nothing: a screenshot or
    a `done` is still something the model did, and the turn after it still has to
    know what comes next.  Leaving either argument optional on some tools is how a
    model learns that it is optional at all.

    Both keys are added last and unconditionally so a future tool cannot ship
    without them by forgetting this helper.
    """
    props = dict(properties)
    props[HISTORY_ARGUMENT] = {
        "type": "string",
        "description": HISTORY_DESCRIPTION,
    }
    props[NEXT_STEP_ARGUMENT] = next_step_schema(NEXT_STEP_DESCRIPTION)
    return {
        "type": "object",
        "properties": props,
        "required": [*list(required), HISTORY_ARGUMENT, NEXT_STEP_ARGUMENT],
    }


def computer_tools(allowed_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return only the tool schemas the current request needs.

    The full tool catalogue is unnecessary on every turn.  The runner narrows
    it for simple tasks so the model has fewer choices and the provider receives
    fewer recurring input tokens.  No new capability is created here: filtering
    only removes schemas from the request.
    """
    object_schema = _object_schema

    schemas = [
        {
            "name": "screenshot",
            "description": "Get the current VM screen. Only when you need to see it.",
            "parameters": object_schema({}, []),
        },
        {
            "name": "navigate",
            "description": "Open a URL in the browser.",
            "parameters": object_schema({"url": {"type": "string", "description": "http(s) URL"}}, ["url"]),
        },
        {
            "name": "search",
            "description": "Open the browser's search engine for a query.",
            "parameters": object_schema({"query": {"type": "string"}}, ["query"]),
        },
        {
            "name": "click",
            "description": "Click a control. Better: element_id from the UI map (e.g. \"V3\"); the point comes from a fresh read of the page, and a stale, unknown, offscreen or disabled id is refused. Otherwise x/y are pixel coordinates from the latest screenshot. target is required either way: a few words naming the control to press, checked against what is really there before the click, so a mismatch is refused.",
            "parameters": object_schema(
                {
                    "element_id": {
                        "type": "string",
                        "description": "An id from the UI map in this request, such as \"V3\" or \"O2\". Preferred over x/y; unknown, stale, offscreen or disabled ids are refused.",
                    },
                    "x": {
                        "type": "number",
                        "description": "Pixel x from the latest screenshot (ignored when element_id is given).",
                    },
                    "y": {
                        "type": "number",
                        "description": "Pixel y from the latest screenshot (ignored when element_id is given).",
                    },
                    "target": {
                        "type": "string",
                        "description": "A few words naming the control to press, such as \"Post button\" or \"search field\". Checked against what is really at that point before the click.",
                    },
                },
                ["target"],
            ),
        },
        {
            "name": "type",
            "description": "Type text into whatever is focused.",
            "parameters": object_schema({"text": {"type": "string"}}, ["text"]),
        },
        {
            "name": "key",
            "description": "Press a key such as ENTER, TAB, ESC or CTRL+L.",
            "parameters": object_schema({"key": {"type": "string"}}, ["key"]),
        },
        {
            "name": "scroll",
            "description": "Scroll the page. Positive is down.",
            "parameters": object_schema({"delta_y": {"type": "integer"}}, ["delta_y"]),
        },
        {
            "name": "done",
            "description": "Finish: the task is complete.",
            "parameters": object_schema({"message": {"type": "string"}}, ["message"]),
        },
        {
            "name": "stop",
            "description": "Terminal stop when the work is done or no more decisions are needed.",
            "parameters": object_schema({"message": {"type": "string"}}, ["message"]),
        },
        {
            "name": "error",
            "description": "Stop: the task cannot be completed safely.",
            "parameters": object_schema({"message": {"type": "string"}}, ["message"]),
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

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
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

    # `history` and `next_step` ride inside the call as metadata for the two memory
    # layers, and are never part of the command.  Dropped here as well as in the
    # runner so that no branch below can pick one up by accident, and so that
    # `Command.to_json()` and the executor both provably receive only the fields
    # the machine is actually driven by.
    args = {
        key: value
        for key, value in args.items()
        if key not in (HISTORY_ARGUMENT, NEXT_STEP_ARGUMENT)
    }

    if name == "screenshot":
        # Carried as a command so it lands in the event log and the trace like
        # every other call, but it has no executor and cannot move anything.
        return Command(type="screenshot"), ""

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
        return Command(type="navigate", url=url), ""

    if name == "search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return None, 'search requires a non-empty string "query"'
        query = query.strip()
        if len(query) > MAX_QUERY_LENGTH:
            return None, f"query is longer than {MAX_QUERY_LENGTH} characters"
        return Command(type="search", query=query), ""

    if name in ("click", "move"):
        # An element id from the UI map is the preferred way to name a control:
        # the map already carries the control's real box, so the point comes
        # from a fresh read of the live page at click time rather than from a
        # number the model estimated out of a picture.  When an id is given,
        # x/y are not merely unnecessary, they are refused as input: two claims
        # about where one control is is exactly the ambiguity the whole design
        # exists to remove.
        raw_element = args.get("element_id")
        if raw_element is not None and not isinstance(raw_element, str):
            return None, 'click element_id must be a string such as "V3"'
        element_id = raw_element.strip() if isinstance(raw_element, str) else ""
        if name == "click" and element_id:
            if not ELEMENT_ID_RE.match(element_id):
                return None, (
                    f"click element_id {element_id!r} is not a UI map id; ids "
                    'look like "V3" (visible) or "O2" (offscreen), exactly as '
                    "written in the UI map"
                )
            raw_target = args.get("target")
            if not isinstance(raw_target, str):
                return None, (
                    'click requires "target": a few words naming the control to '
                    'press, such as "Post button"'
                )
            target = clean_target(raw_target)
            if not target:
                return None, (
                    'click requires a non-empty "target" naming the control to '
                    'press, such as "Post button"'
                )
            return Command(type="click", target=target, element_id=element_id), ""
        x = _finite_number(args.get("x"))
        y = _finite_number(args.get("y"))
        if x is None or y is None:
            return None, f"{name} requires numeric x and y"
        # Some OpenAI-compatible models emit normalized coordinates such as
        # {"x":"0.7449","y":"0.4085"} even when the schema says "number".
        # Convert that common representation once, locally, instead of paying
        # for another model request just to re-express the same click.
        ix, iy = int(round(x)), int(round(y))
        if bounds is not None and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and (x < 1.0 or y < 1.0):
            ix = int(round(x * float(bounds.width)))
            iy = int(round(y * float(bounds.height)))
        if bounds is not None and not bounds.contains(ix, iy):
            # Refused against the size of the screenshot the model was shown,
            # not against a configured screen size: a coordinate is only
            # meaningful in the grid it was read from.
            #
            # The bounds are spelled out as ranges rather than left as "outside
            # the screenshot", because the model has to pick a different point
            # from that same image and a difference is only actionable if it is
            # told what the valid values are.  It is not told to take a
            # screenshot: the one that proved the point is attached to this very
            # request and survives the refusal, so asking for another spends a
            # request to deliver the frame that is already in the message.
            return None, (
                f"{name} at ({ix}, {iy}) is outside the "
                f"{bounds.width}x{bounds.height} screenshot and was not performed; "
                f"valid coordinates are x from 0 to {int(bounds.width) - 1} and y "
                f"from 0 to {int(bounds.height) - 1}, read off the screenshot in "
                f"this message"
            )
        # `target` is the claim being made about this coordinate, and it is only
        # a claim for a click: naming a control to move the pointer to would
        # promise something the click check never verifies.  Required for a
        # click: a coordinate without a claim about what belongs there is a
        # click on whatever happens to be under it, and the check downstream has
        # nothing to check.
        target = ""
        if name == "click":
            raw_target = args.get("target")
            if not isinstance(raw_target, str):
                return None, (
                    'click requires "target": a few words naming the control to '
                    'press, such as "Post button"'
                )
            target = clean_target(raw_target)
            if not target:
                return None, (
                    'click requires a non-empty "target" naming the control to '
                    'press, such as "Post button"'
                )
        return Command(type=name, x=float(ix), y=float(iy), target=target), ""

    if name == "type":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            return None, 'type requires a non-empty string "text"'
        if "\x00" in text:
            return None, "type text may not contain a null byte"
        if len(text) > MAX_TEXT_LENGTH:
            return None, f"text is longer than {MAX_TEXT_LENGTH} characters"
        return Command(type="type", text=text), ""

    if name == "key":
        combo, error = normalize_key(args.get("key"))
        if error:
            return None, error
        return Command(type="key", key=combo), ""

    if name == "scroll":
        delta = _finite_number(args.get("delta_y"))
        if delta is None:
            return None, "scroll requires a numeric delta_y"
        from .commands import MAX_SCROLL_DELTA

        if abs(delta) > MAX_SCROLL_DELTA:
            return None, f"delta_y must be between -{MAX_SCROLL_DELTA} and {MAX_SCROLL_DELTA}"
        return Command(type="scroll", delta_y=int(delta)), ""

    # done / error
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return None, f'{name} requires a non-empty string "message"'
    from .commands import MAX_MESSAGE_LENGTH

    message = message.strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        return None, f"message is longer than {MAX_MESSAGE_LENGTH} characters"
    return Command(type=name, message=message), ""


__all__ = [
    "TOOL_NAMES",
    "STATE_CHANGING_TOOLS",
    "HISTORY_ARGUMENT",
    "HISTORY_DESCRIPTION",
    "computer_tools",
    "parse_arguments",
    "tool_to_command",
    "ALLOWED_TYPES",
    "FIRST_TURN_TYPES",
    "KEY_ALLOWLIST",
    "MODIFIER_ALLOWLIST",
]
