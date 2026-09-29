"""Parsing and validating the model's computer-control command.

Pure functions only: no network, no database, no agent.  Everything the loop is
allowed to do is decided here, and a test can prove the boundary holds without a
browser or an API key.

The central rule is that this module never *interprets*.  It accepts a
whitelist of five commands with the fields each one needs, and rejects
everything else without guessing what was meant.  A model that says "click the
login button" has not issued a command, and a model that says
`{"type":"run","cmd":"rm -rf /"}` has issued one we do not have.  Both are
refused the same way: nothing runs, and the reason goes back to the model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

#: The complete set of capabilities in this phase.  Deliberately tiny: every
#: addition here is a new thing a language model can be talked into doing on a
#: machine that has a signed-in browser session.
ALLOWED_TYPES = ("navigate", "search", "click", "done", "error")

#: The first turn has no screenshot, so there is no coordinate that could have
#: come from one.  A click there is either a hallucination or a leftover from a
#: previous conversation, and both are refused.
#:
#: "error" is allowed on the first turn even though it is not an action.  A model
#: that opens with "I can't do that" is being honest, and the alternative -- an
#: allowlist that only accepts navigate and search -- would force it to navigate
#: somewhere arbitrary first and reason from there.  "done" is still refused:
#: nothing has been done yet, so it would be an unevidenced claim.
FIRST_TURN_TYPES = ("navigate", "search", "error")

MAX_URL_LENGTH = 2048
MAX_QUERY_LENGTH = 512
MAX_MESSAGE_LENGTH = 2000


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
    message: str = ""

    def to_json(self) -> Dict[str, Any]:
        """The canonical record of what was issued, for the event log."""
        if self.type == "navigate":
            return {"type": "navigate", "url": self.url}
        if self.type == "search":
            return {"type": "search", "query": self.query}
        if self.type == "click":
            return {"type": "click", "x": self.x, "y": self.y}
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

    if kind == "click":
        x = _finite_number(data.get("x"))
        y = _finite_number(data.get("y"))
        if x is None or y is None:
            return None, "click requires finite numeric \"x\" and \"y\""
        if bounds is None:
            return None, "internal: a click was parsed without screenshot bounds"
        if not bounds.contains(x, y):
            return None, (
                f"({x:g}, {y:g}) is outside the {bounds.width}x{bounds.height} "
                "screenshot; coordinates must come from the latest screenshot"
            )
        return Command(type="click", x=x, y=y), ""

    message = data.get("message")
    if message is None:
        message = ""
    if not isinstance(message, str):
        return None, '"message" must be a string'
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[:MAX_MESSAGE_LENGTH]
    return Command(type=kind, message=message), ""
