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

import json
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
    ``value_length`` is carried the same way: an int means the field's text was
    counted, and an absent key means the agent did not count it.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {"role": _clean(raw.get("role")), "name": _clean(raw.get("name"))}
    if isinstance(raw.get("editable"), bool):
        out["editable"] = raw["editable"]
    vl = raw.get("value_length")
    if isinstance(vl, int) and not isinstance(vl, bool):
        out["value_length"] = int(vl)
    return out


def _sig_field(raw: Any) -> list:
    """One descriptor as a comparable list, empty fields as None.

    Nothing here is for display: it is what a signature compares, so a field
    the agent legitimately did not report (``None``) compares differently from
    one it reported as empty, and editable/value_length ride along where the
    agent answered them.
    """
    d = _descriptor(raw)
    return [
        d.get("role") or None,
        d.get("name") or None,
        d.get("editable") if isinstance(d.get("editable"), bool) else None,
        d.get("value_length") if isinstance(d.get("value_length"), int) else None,
    ]


def state_signature(state: Optional[Dict[str, Any]]) -> str:
    """The page as a stable string two reads can be compared on.

    Distinct from `format_ui_state`: that is for the model, this is for the
    loop.  Every field that could move when an action lands is in the signature
    -- url, title, the focused node and how much text it holds, the caret's
    target, dialog, selection and scroll -- and nothing that does not move is,
    so an unchanged signature is a truthful "the page did not change", which is
    the evidence a repeated action is spinning rather than working.
    """
    if not isinstance(state, dict):
        return ""
    focused = state.get("focused")
    scroll = state.get("scroll")
    scroll = scroll if isinstance(scroll, dict) else {}
    body = {
        "url": _clean(state.get("url")),
        "title": _clean(state.get("title")),
        "focused": focused if isinstance(focused, bool) else None,
        "focus": _sig_field(state.get("focus")),
        "caret": _sig_field(state.get("caret")),
        "dialog": _clean(state.get("dialog", {}).get("name")) if isinstance(state.get("dialog"), dict) else None,
        "selected": _sig_field(state.get("selected")),
        "scroll": [scroll.get("x"), scroll.get("y")],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


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
        editable = "yes" if focus["editable"] else "no"
        if focus.get("editable") and isinstance(focus.get("value_length"), int):
            editable += f" ({focus['value_length']} characters)"
        lines.append(f"- Editable: {editable}")
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

    # The caret's editable field, reported by the agent only when the caret is
    # *in* an editable node that is not the focused one.  The focused element
    # carries its own "Editable: yes" when it is the text receiver, so this line
    # exists precisely for the case that is otherwise hidden: focus is on a tab
    # or a wrapper while the caret is in the composer that was just clicked.
    caret = _descriptor(state.get("caret"))
    if caret.get("role") and not focus.get("editable"):
        label = "Last focused editable field" if focused is False else "Focused editable field"
        line = f"- {label}: {caret['role']}"
        if caret.get("name"):
            line += f' "{caret["name"]}"'
        if isinstance(caret.get("value_length"), int):
            line += f" ({caret['value_length']} characters)"
        lines.append(line)

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


__all__ = ["MAX_NAME_CHARS", "UI_STATE_UNAVAILABLE", "format_ui_state", "state_signature"]
