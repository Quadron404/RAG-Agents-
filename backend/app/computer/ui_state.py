"""The page's real state, as text for the model.

The loop already sends the model two things about the past -- ``History.txt``,
which it wrote itself, and the executor's verdict on its last action -- and one
thing about the present, the screenshot.  All three are consistent with a model
that has already acted on a control and still sees it: the composer it just
clicked is in the picture, its own memory says it clicked it, and the executor
says the click worked.  Nothing told it the click had already *focused* the
field, because that fact is not visible in a picture of a textbox -- an
unfocused and a focused textbox are the same rectangle.

So this module carries what a screenshot cannot: the state of the page itself,
read from the live DOM over CDP by the agent and never written by the model.

Two rules make it trustworthy:

- every field is reported or omitted, and an omitted field is genuinely absent
  from the DOM rather than filled in by a guess.  A page that could not be read
  reports nothing rather than something convenient;
- only the newest read is ever rendered.  A state read before the last action is
  a different page, and sending it alongside a fresher screenshot is the one
  combination that would be actively misleading.

Nothing here is site-specific.  There is no URL pattern, no control name and no
per-site special case: the fields come from ARIA and the platform's own implicit
role mapping, so the same block describes any page.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Sent instead of a state that could not be read.  Distinct from "read, and the
#: page has nothing focused": this says the browser did not answer.
UI_STATE_UNAVAILABLE = (
    "Current UI state: unavailable -- the browser did not report its state, so "
    "treat the page as unknown and read it from the screenshot."
)

#: Longest accessible name worth sending.  A name is a label, not a paragraph;
#: a container's text content can be the whole page, and this is sent on every
#: request of the run.
MAX_NAME_CHARS = 120


def _clean(value: Any) -> str:
    """One field as a single line, or "" when it has nothing in it."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_NAME_CHARS].strip()


def _descriptor(raw: Any) -> Dict[str, str]:
    """One ``{role, name}`` pair from the agent, or empty when there is none."""
    if not isinstance(raw, dict):
        return {}
    return {"role": _clean(raw.get("role")), "name": _clean(raw.get("name"))}


def format_ui_state(state: Optional[Dict[str, Any]]) -> str:
    """The block the model reads, or "" when there is nothing true to say.

    Returns "" for a read that produced no fact at all -- an unreachable browser,
    an empty target -- so the caller sends no block instead of an empty one.  The
    model is never told a field it was not given.
    """
    if not isinstance(state, dict):
        return ""

    lines = ["Current UI state:"]

    url = _clean(state.get("url"))
    if url:
        lines.append(f"- URL: {url}")

    focus = _descriptor(state.get("focus"))
    if focus.get("role"):
        lines.append(f"- Focused element: {focus['role']}")
        if focus.get("name"):
            lines.append(f'- Accessible name: "{focus["name"]}"')

    dialog = _descriptor(state.get("dialog"))
    if dialog:
        label = f'"{dialog["name"]}"' if dialog.get("name") else "open"
        lines.append(f"- Dialog: {label}")

    selected = _descriptor(state.get("selected"))
    if selected.get("role"):
        lines.append(f"- Active/selected element: {selected['role']}")
        if selected.get("name"):
            # Labelled apart from the focused element's name: a page can have both
            # at once, and two identical "Accessible name" lines would leave the
            # model to guess which one it is reading.
            lines.append(f'- Selected element name: "{selected["name"]}"')

    # A read that says nothing at all is not a page with nothing on it.
    if len(lines) == 1:
        return ""
    lines.append(
        "Read from the live browser. A field that is absent could not be read, "
        "not that it does not exist."
    )
    return "\n".join(lines)


__all__ = ["MAX_NAME_CHARS", "UI_STATE_UNAVAILABLE", "format_ui_state"]
