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

Focus in particular is reported as two facts, not one.  Which node holds the
caret and whether the page holds focus at all are separate questions, and a page
without focus still names its *last* focused node while ignoring everything
typed now.  Sending the node without that qualifier is how a correct reading
becomes a wrong claim: the model is told a tab is focused, concludes the field
it just clicked never took focus, and clicks it again.  The window state travels
with the node, and an unfocused node is labelled as the last one focused rather
than as the current one.  Whether that node accepts text is the third fact, and
it is the one that decides whether the next action is a type or another click.

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


def _descriptor(raw: Any) -> Dict[str, Any]:
    """One ``{role, name}`` pair from the agent, or empty when there is none.

    ``editable`` is carried only when the agent actually answered it: a bool
    means "read, and this is the answer", an absent key means "not reported",
    and the caller leaves the line out rather than inventing one.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {"role": _clean(raw.get("role")), "name": _clean(raw.get("name"))}
    if isinstance(raw.get("editable"), bool):
        out["editable"] = raw["editable"]
    return out


def _focus_lines(raw: Any, focused: Any) -> list:
    """The focus block, labelled for whether the page holds focus now.

    ``focused`` is tri-state: ``True``/``False`` is what the page reported,
    ``None`` is a page that could not answer.  Only ``False`` changes the
    label -- a node reported while the page is unfocused is where focus *was*,
    and typing would not reach it.  An unknown page state keeps the plain label
    because neither claim can be made, and the window line is left out.
    """
    focus = _descriptor(raw)
    if not focus.get("role"):
        return []

    last = focused is False
    lines = [
        f"- {'Last focused element' if last else 'Focused element'}: {focus['role']}"
    ]
    if focus.get("name"):
        lines.append(f'- Accessible name: "{focus["name"]}"')
    if "editable" in focus:
        lines.append(f"- Editable: {'yes' if focus['editable'] else 'no'}")
    return lines


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

    focused = state.get("focused")
    if isinstance(focused, bool):
        lines.append(f"- Browser window focused: {'yes' if focused else 'no'}")

    focus_raw = state.get("focus")
    focus = _descriptor(focus_raw)
    focus_lines = _focus_lines(focus_raw, focused)
    if focus_lines:
        lines.extend(focus_lines)

    dialog = _descriptor(state.get("dialog"))
    if dialog:
        label = f'"{dialog["name"]}"' if dialog.get("name") else "open"
        lines.append(f"- Dialog: {label}")

    selected = _descriptor(state.get("selected"))
    # A selected element that is the focused element is already reported above;
    # repeating it under a second label reads as two facts about the page when
    # it is one.
    same_as_focus = (
        selected.get("role") == focus.get("role")
        and selected.get("name") == focus.get("name")
    )
    if selected.get("role") and not same_as_focus:
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
