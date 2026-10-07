"""Parsing and validating the model's computer-control command.

Pure functions only: no network, no database, no agent.  Everything the loop is
allowed to do is decided here, and a test can prove the boundary holds without a
browser or an API key.

The central rule is that this module never *interprets*.  It accepts a
whitelist of nine commands with the fields each one needs, and rejects
everything else without guessing what was meant.  A model that says "click the
login button" has not issued a command, and a model that says
`{"type":"run","cmd":"rm -rf /"}` has issued one we do not have.  Both are
refused the same way: nothing runs, and the reason goes back to the model.

Keyboard input is the sharpest edge here, because it is the one command that
carries a free-form string all the way to a keyboard.  It is handled by
translating a *name* into a fixed table, never by passing a model's string
through: `{"type":"key","key":"CTRL+L"}` selects two entries from
MODIFIER_ALLOWLIST and KEY_ALLOWLIST, and there is no input for which any part
of the model's text becomes an argument.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .hit_target import clean_target

#: The complete set of capabilities in this phase.  Every entry maps to exactly
#: one method on RemoteComputer and one route on the agent, so the allowlist
#: cannot grow a capability the loop has no way to perform.
ALLOWED_TYPES = (
    "navigate",
    "search",
    "click",
    "type",
    "key",
    "scroll",
    "move",
    "done",
    "stop",
    "error",
)

#: The first turn has no screenshot, so there is no coordinate that could have
#: come from one.  A click or a move there is either a hallucination or a
#: leftover from a previous conversation, and both are refused.
#:
#: "error" is allowed on the first turn even though it is not an action.  A model
#: that opens with "I can't do that" is being honest, and the alternative -- an
#: allowlist that only accepts navigate and search -- would force it to navigate
#: somewhere arbitrary first and reason from there.  "done" is still refused:
#: nothing has been done yet, so it would be an unevidenced claim.
FIRST_TURN_TYPES = ("navigate", "search", "error")

#: What may be done to something that is *visible*.  Narrower than
#: ALLOWED_TYPES on purpose, and the difference is the whole point: once there
#: is a screenshot, `navigate` and `search` stop being the answer to "the
#: button is right there".  They stay in the allowlist because re-navigating is
#: still legitimate when the task really needs another page, and hiding that
#: would make the inspector disagree with the parser.  Kept here, beside
#: ALLOWED_TYPES, so the two lists cannot drift apart silently.
SCREENSHOT_ACTIONS = (
    "click",
    "type",
    "key",
    "scroll",
    "move",
    "done",
    "stop",
    "error",
)

MAX_URL_LENGTH = 2048
MAX_QUERY_LENGTH = 512
MAX_MESSAGE_LENGTH = 2000
#: One type command is bounded so a model cannot use the keyboard to write an
#: unbounded amount into whatever happens to have focus.  Long text is typed in
#: several commands instead.
MAX_TEXT_LENGTH = 2000

#: The keyboard allowlist, duplicated from the agent on purpose.
#:
#: Two deployables means two copies of any allowlist, and that is the correct
#: arrangement here rather than a shortcut: the agent is the boundary that
#: actually touches the machine and enforces this again regardless of what
#: reaches it, while this copy exists so a bad key name is refused with a
#: message that can go straight back to the model.  If the two ever disagree the
#: agent wins, and the model's request simply fails with the agent's reason.
KEY_ALLOWLIST: Dict[str, str] = {
    "ENTER": "Return",
    "RETURN": "Return",
    "TAB": "Tab",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "SPACE": "space",
    "BACKSPACE": "BackSpace",
    "DELETE": "Delete",
    "DEL": "Delete",
    "INSERT": "Insert",
    "HOME": "Home",
    "END": "End",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    # Both spellings, because a model reaching for an arrow key is as likely to
    # say ARROWDOWN as DOWN and a refusal there reads as a broken keyboard.
    "ARROWUP": "Up",
    "ARROWDOWN": "Down",
    "ARROWLEFT": "Left",
    "ARROWRIGHT": "Right",
    "UPARROW": "Up",
    "DOWNARROW": "Down",
    "LEFTARROW": "Left",
    "RIGHTARROW": "Right",
    "PAGEUP": "Prior",
    "PAGEDOWN": "Next",
    "PRIOR": "Prior",
    "NEXT": "Next",
    "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4",
    "F5": "F5", "F6": "F6", "F7": "F7", "F8": "F8",
    "F9": "F9", "F10": "F10", "F11": "F11", "F12": "F12",
}

#: Modifiers allowed in front of a key, for combos such as CTRL+L.
MODIFIER_ALLOWLIST: Dict[str, str] = {
    "CTRL": "ctrl",
    "CONTROL": "ctrl",
    "ALT": "alt",
    "SHIFT": "shift",
    "META": "super",
    "SUPER": "super",
}

#: A single ASCII letter or digit is a valid key, so CTRL+A and CTRL+L work.
SINGLE_KEY_ALLOWLIST = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

#: Scrolling further than this in one command is refused rather than clamped:
#: a model asking for 100000 pixels has lost track of the page, and a clamped
#: scroll would hide that.
MAX_SCROLL_DELTA = 5000



@dataclass
class Bounds:
    """The size of the screenshot the coordinates are measured against."""

    width: int
    height: int

    def contains(self, x: float, y: float) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height


@dataclass
class Command:
    type: str
    url: str = ""
    query: str = ""
    x: float = 0.0
    y: float = 0.0
    text: str = ""
    key: str = ""
    delta_y: int = 0
    message: str = ""
    #: The few words a click claimed to be aiming at, checked against what the
    #: page actually holds at the point before the click runs.  Empty for every
    #: command that is not a click -- a click itself never has it empty, since
    #: both parsers refuse one that names nothing, and there is no promise to
    #: contradict when there was no promise.
    #:
    #: Deliberately absent from `to_json()`: the log records what the machine
    #: was asked to do, which is a coordinate, and `target` is a claim the
    #: model made about that coordinate rather than part of the command.
    target: str = ""

    def to_json(self) -> Dict[str, Any]:
        """The canonical record of what was issued, for the event log.

        Only the fields the command actually uses are recorded, so a click in
        the log never carries a stale "url" from the dataclass default.
        """
        if self.type == "navigate":
            return {"type": "navigate", "url": self.url}
        if self.type == "search":
            return {"type": "search", "query": self.query}
        if self.type in ("click", "move"):
            return {"type": self.type, "x": self.x, "y": self.y}
        if self.type == "type":
            return {"type": "type", "text": self.text}
        if self.type == "key":
            return {"type": "key", "key": self.key}
        if self.type == "scroll":
            return {"type": "scroll", "delta_y": self.delta_y}
        return {"type": self.type, "message": self.message}


def _finite_number(value: Any) -> Optional[float]:
    """A real, finite number.

    bool is rejected explicitly: in Python `True` is an int, and without this a
    model sending {"x": true} would be read as a click at (1, 0).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def extract_json(text: str) -> Optional[Any]:
    """Pull the single JSON value out of a model reply.

    Strict by design: `json.loads` on the whole string first, and only if that
    fails, one narrow rescue attempt for a reply that wrapped its object in a
    markdown fence.  A fence is stripped because it is a formatting artefact,
    not an instruction -- but nothing else is stripped.  Leading prose,
    trailing commentary and a second JSON object are all still refusals, because
    accepting them would mean executing text the model wrote *around* the
    command.
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # The one tolerated wrapper.  Requires a fence on the first line, so plain
    # prose followed by JSON does not qualify.
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            body = "\n".join(lines[1:])
            if body.rstrip().endswith("```"):
                body = body.rstrip()[:-3]
            try:
                return json.loads(body.strip())
            except json.JSONDecodeError:
                return None
    return None


def normalize_key(raw: Any) -> Tuple[str, str]:
    """Turn a requested key name into a validated xdotool combo.

    Returns ``(combo, error)``, exactly one of which is meaningful.  The combo is
    built entirely from KEY_ALLOWLIST and MODIFIER_ALLOWLIST and joined with "+",
    so nothing a model sends becomes an argument here: the worst it can do is
    produce a name that is not in a table, and that is refused.
    """
    if not isinstance(raw, str):
        return "", '"key" must be a string'
    name = raw.strip()
    if not name:
        return "", "key requires a non-empty key name"

    # A hyphen is the other way people write these, so CTRL-L means CTRL+L.
    parts = [p for p in name.replace("-", "+").split("+") if p.strip()]
    if not parts:
        return "", "key requires a non-empty key name"
    parts = [p.strip().upper() for p in parts]
    if len(parts) > 3:
        return "", "a key combo may have at most two modifiers and one key"

    base = parts[-1]
    if len(base) == 1:
        keysym = base.lower()
        if keysym not in SINGLE_KEY_ALLOWLIST:
            return "", f"{base!r} is not an allowed key"
    elif base in KEY_ALLOWLIST:
        keysym = KEY_ALLOWLIST[base]
    else:
        return "", (
            f"{base!r} is not an allowed key. Allowed: "
            f"{', '.join(sorted(KEY_ALLOWLIST))}, or CTRL/ALT/SHIFT/META plus one "
            "of those, or a single letter or digit"
        )

    resolved: list = []
    seen: set = set()
    for mod in parts[:-1]:
        if mod not in MODIFIER_ALLOWLIST:
            return "", (
                f"{mod!r} is not an allowed modifier. Allowed: "
                f"{', '.join(sorted(set(MODIFIER_ALLOWLIST)))}"
            )
        token = MODIFIER_ALLOWLIST[mod]
        if token not in seen:
            seen.add(token)
            resolved.append(token)
    return "+".join([*resolved, keysym]), ""


def parse_command(
    raw_text: str,
    *,
    bounds: Optional[Bounds] = None,
    first_turn: bool = False,
) -> Tuple[Optional[Command], str]:
    """Validate one model reply.

    Returns ``(command, error)``.  Exactly one is meaningful: a command means the
    reply was valid and safe to execute, and an error string means nothing
    happened and the caller should correct the model.

    `bounds` is the size of the screenshot the model was actually shown.  Passing
    it is what makes "reject coordinates outside the current screenshot bounds"
    true rather than aspirational: a click is checked against the image it was
    read from, and the loop always passes the newest one.
    """
    data = extract_json(raw_text)
    if data is None:
        return None, "the reply was not a single valid JSON object"
    if not isinstance(data, dict):
        return None, f"expected a JSON object, got {type(data).__name__}"

    kind = data.get("type")
    if not isinstance(kind, str):
        return None, 'missing a string "type" field'
    kind = kind.strip().lower()
    if kind not in ALLOWED_TYPES:
        return None, f'"type": {kind!r} is not one of {", ".join(ALLOWED_TYPES)}'

    if first_turn and kind not in FIRST_TURN_TYPES:
        return None, (
            f"the first command must be navigate or search, not {kind!r}: "
            "there is no screenshot yet, so there is no coordinate to click"
        )

    if kind == "stop":
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            return None, 'stop requires a non-empty string "message"'
        return Command(type="stop", message=message.strip()), ""

    if kind == "navigate":
        url = data.get("url")
        if not isinstance(url, str) or not url.strip():
            return None, "navigate requires a non-empty string \"url\""
        url = url.strip()
        if len(url) > MAX_URL_LENGTH:
            return None, f"url is longer than {MAX_URL_LENGTH} characters"
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None, f"only http and https urls are allowed, not {parsed.scheme or 'no'} scheme"
        if not parsed.netloc:
            return None, "url has no host"
        return Command(type="navigate", url=url), ""

    if kind == "search":
        query = data.get("query")
        if not isinstance(query, str) or not query.strip():
            return None, "search requires a non-empty string \"query\""
        query = query.strip()
        if len(query) > MAX_QUERY_LENGTH:
            return None, f"query is longer than {MAX_QUERY_LENGTH} characters"
        return Command(type="search", query=query), ""

    if kind in ("click", "move"):
        x = _finite_number(data.get("x"))
        y = _finite_number(data.get("y"))
        if x is None or y is None:
            return None, f'{kind} requires finite numeric "x" and "y"'
        if bounds is None:
            return None, f"internal: a {kind} was parsed without screenshot bounds"
        if not bounds.contains(x, y):
            # Same wording as the native-tool refusal in `tools.py`, on purpose:
            # the two validators can refuse the same click, and a model told two
            # different things about one mistake reads the difference as a new
            # problem instead of as the error it is.
            return None, (
                f"({x:g}, {y:g}) is outside the {bounds.width}x{bounds.height} "
                f"screenshot and {kind} was not performed; valid coordinates are "
                f"x from 0 to {int(bounds.width) - 1} and y from 0 to "
                f"{int(bounds.height) - 1}, read off the latest screenshot"
            )
        target = ""
        if kind == "click":
            raw_target = data.get("target")
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
        return Command(type=kind, x=x, y=y, target=target), ""

    if kind == "type":
        text = data.get("text")
        if not isinstance(text, str):
            return None, 'type requires a string "text"'
        if not text:
            return None, 'type requires a non-empty "text"'
        if "\x00" in text:
            return None, "text cannot contain a null byte"
        if len(text) > MAX_TEXT_LENGTH:
            return None, (
                f"text is longer than {MAX_TEXT_LENGTH} characters; "
                "send it as several type commands"
            )
        return Command(type="type", text=text), ""

    if kind == "key":
        combo, error = normalize_key(data.get("key"))
        if error:
            return None, error
        return Command(type="key", key=combo), ""

    if kind == "scroll":
        delta = _finite_number(data.get("delta_y"))
        if delta is None:
            return None, 'scroll requires a finite numeric "delta_y"'
        delta = int(delta)
        if delta == 0:
            return None, 'scroll requires a non-zero "delta_y"; positive scrolls down, negative scrolls up'
        if abs(delta) > MAX_SCROLL_DELTA:
            return None, (
                f"delta_y must be between -{MAX_SCROLL_DELTA} and "
                f"{MAX_SCROLL_DELTA}; scroll in several commands instead"
            )
        return Command(type="scroll", delta_y=delta), ""

    message = data.get("message")
    if message is None:
        message = ""
    if not isinstance(message, str):
        return None, '"message" must be a string'
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[:MAX_MESSAGE_LENGTH]
    return Command(type=kind, message=message), ""
