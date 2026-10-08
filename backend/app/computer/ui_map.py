"""The UI map: what the live DOM says it is showing, as text for the model.

The loop already sends the model a screenshot -- pixels -- and a state block --
the page's own words about focus, URL and scroll.  What neither of them can do
is answer "which control should I press" without the model estimating a
coordinate from an image, which is the one thing a vision model is worst at: a
box that reads as 12 pixels off in a screenshot is a click that lands on the
neighbour.

So this module turns the agent's raw read of the page into two things:

- a **map** -- a bounded, ranked list of controls with the box each one
  currently occupies, in the same display pixels the screenshot and the click
  live in, each carrying a short per-snapshot id (`V3`, `O1`) the model can
  name back;
- a **resolution** -- given an id the model chose and a fresh read of the page
  taken a moment later, the exact point to click, or a reason not to click.

Four rules keep it honest:

- Ids are per snapshot and are never remapped.  An id means "the entry this
  request's map showed", and an id that no longer resolves -- the page
  navigated, the control moved, the entry is gone -- is refused rather than
  pointed at whatever now sits in its position.  Stale is the failure mode
  this design exists to remove; reinterpreting an id would reintroduce it.
- Visible, offscreen and hidden are three different answers.  A visible entry
  carries a real box clipped to what is actually on screen; an offscreen entry
  carries no box at all (a coordinate for something not on the screen is a
  guess) but carries direction and distance so the model can scroll to it;
  a hidden entry -- display:none, visibility:hidden, zero-size -- is not
  rendered and therefore not mapped, because it cannot be clicked.
- The model never sees raw output.  The agent reports everything it found, in
  document order; this module decides what is worth sending (biggest visible
  controls first, nearest offscreen first, hard caps) and stamps ids only on
  that choice, so an id is always bounded and always means one of the entries
  the model was actually shown.
- Resolution re-reads the page instead of trusting the map.  The box in the
  request is where the control *was*; the click point is the centre of where
  it is now, with role, name, tag, type and text compared in between so a
  control that changed underneath the id is refused, not reinterpreted.

Nothing here is site-specific: roles come from ARIA and the platform's own
implicit role mapping, boxes from the DOM's own geometry, and the same block
describes any page.  This module itself is pure -- it never talks to the
browser; the agent's reply comes in as a dict and the model's text goes out
as a string.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

#: What a model-issued element id looks like.  `V` for a visible entry (has a
#: box), `O` for an offscreen one (does not), digits so the id stays short
#: enough to read back without transcription errors.  Explicitly not a bare
#: number: a bare number would look like a DOM index, and DOM indexes are
#: exactly the position-based identity that goes stale silently.
ELEMENT_ID_RE = re.compile(r"^[VO][0-9]{1,4}$")

#: How many visible entries the model is shown.  Forty covers a dense page's
#: real controls while keeping the block smaller than the screenshot it sits
#: next to; the selection is by area, so the things a person would actually
#: press -- the primary button, the nav links -- are the ones that survive.
MAX_VISIBLE_ENTRIES = 40

#: How many offscreen entries the model is shown, nearest first.  They cost
#: more text than a visible entry (no box to point at) and are less likely to
#: be the next action, so they get the smaller budget.
MAX_OFFSCREEN_ENTRIES = 20

#: Longest name on one line.  A name is a label, not a paragraph: containers
#: fall back to their text content, and that can be the whole page.
MAX_NAME_CHARS = 60

#: Longest text carried for identity comparison.  Longer than the displayed
#: name on purpose: two controls can share a short label and differ in their
#: text, and identity is what decides whether a click is still safe.
MAX_TEXT_CHARS = 100


def _clean(value: Any, limit: int) -> str:
    """One field as a single line, or "" when it has nothing in it."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def _label(entry: Dict[str, Any]) -> str:
    """What to call this entry when its name would be redundant or absent."""
    return entry.get("role") or entry.get("tag") or "element"


def describe_entry(entry: Optional[Dict[str, Any]]) -> str:
    """One entry as a short phrase, for a refusal message.

    The form is `<role> "<name>"` when there is a name and the bare role
    otherwise -- enough for a model to recognise which control a message is
    about without the message carrying the entry's whole line.
    """
    if not isinstance(entry, dict):
        return "element"
    label = _label(entry)
    name = entry.get("name") or ""
    return f'{label} "{name}"' if name else label


def _identity(entry: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    """The fields that make an id's target the same control it was.

    Role, name, tag, type and text: what a person would use to say "yes, that
    is still the Send button".  Box is deliberately absent -- controls move
    when a banner appears and that is not a different control -- and ids are
    absent because comparing an id to itself proves nothing.
    """
    return (
        entry.get("role") or "",
        entry.get("name") or "",
        entry.get("tag") or "",
        entry.get("type") or "",
        entry.get("text") or "",
    )


def _valid_box(raw: Any) -> Optional[Dict[str, int]]:
    """A box as four ints with a non-zero size, or None when it is unusable."""
    if not isinstance(raw, dict):
        return None
    try:
        x = int(raw.get("x"))
        y = int(raw.get("y"))
        width = int(raw.get("width"))
        height = int(raw.get("height"))
    except (TypeError, ValueError):
        return None
    if width < 1 or height < 1:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _clamp_box(box: Dict[str, int], display_w: int, display_h: int) -> Dict[str, int]:
    """Keep a box on the display the screenshot was taken of."""
    if display_w > 0:
        box["x"] = max(0, min(box["x"], display_w - 1))
        box["width"] = max(1, min(box["width"], display_w - box["x"]))
    if display_h > 0:
        box["y"] = max(0, min(box["y"], display_h - 1))
        box["height"] = max(1, min(box["height"], display_h - box["y"]))
    return box


def _entry(raw: Any) -> Optional[Dict[str, Any]]:
    """One raw entry from the agent as a usable one, or None.

    Role and name are cleaned rather than trusted: they are strings the page
    wrote, and this line goes to the model unchanged, so a name full of
    newlines would break the map into lines it does not have.
    """
    if not isinstance(raw, dict):
        return None
    role = _clean(raw.get("role"), 40)
    name = _clean(raw.get("name"), MAX_NAME_CHARS)
    tag = _clean(raw.get("tag"), 20)
    if not role and not name and not tag:
        return None
    out: Dict[str, Any] = {
        "role": role,
        "name": name,
        "text": _clean(raw.get("text"), MAX_TEXT_CHARS),
        "tag": tag,
        "type": _clean(raw.get("type"), 40),
        "editable": bool(raw.get("editable")),
        "disabled": bool(raw.get("disabled")),
    }
    box = _valid_box(raw.get("box"))
    if box is not None:
        out["box"] = box
    else:
        off = _clean(raw.get("off"), 10)
        if off not in ("below", "above", "left", "right"):
            off = "below"
        dist = raw.get("dist")
        out["off"] = off
        out["dist"] = dist if isinstance(dist, int) and not isinstance(dist, bool) and dist >= 0 else 0
        out["scroller"] = "panel" if raw.get("scroller") else ""
    return out


def build_ui_map(raw: Any) -> Dict[str, Any]:
    """The agent's raw read as the map the model will be shown.

    Selection, capping and id assignment all happen here rather than in the
    agent: the agent reports what exists, and what the model sees is this
    module's decision -- biggest visible controls first (they are what the
    screenshot shows largest and what a person presses first), nearest
    offscreen controls first (they are the ones a scroll can reach), hard caps
    on both, and the chosen ones re-sorted into document order so the map
    reads down the page the way the page does.

    Ids are stamped after selection: `V1..Vn` over the visible choice,
    `O1..Om` over the offscreen choice, so an id's number is also a rough
    statement of priority.  Disabled offscreen controls are dropped -- a
    control that is out of view and cannot be acted on is not worth one of the
    twenty slots -- while disabled visible ones are kept and marked, because
    the model can see them in the screenshot and should be told why clicking
    them will be refused.

    Returns `{}` when the agent's answer cannot be used, which the caller
    treats as "no map this turn" rather than as an empty page.
    """
    if not isinstance(raw, dict) or not raw.get("ok"):
        return {}
    try:
        display_w = int(raw.get("display_width") or 0)
        display_h = int(raw.get("display_height") or 0)
    except (TypeError, ValueError):
        display_w, display_h = 0, 0

    visible: List[Tuple[int, Dict[str, Any]]] = []
    raw_visible = raw.get("visible")
    if isinstance(raw_visible, list):
        for index, candidate in enumerate(raw_visible[:400]):
            entry = _entry(candidate)
            if entry is None or "box" not in entry:
                continue
            _clamp_box(entry["box"], display_w, display_h)
            visible.append((index, entry))

    offscreen: List[Tuple[int, Dict[str, Any]]] = []
    raw_offscreen = raw.get("offscreen")
    if isinstance(raw_offscreen, list):
        for index, candidate in enumerate(raw_offscreen[:400]):
            entry = _entry(candidate)
            if entry is None or "box" in entry:
                continue
            if entry.get("disabled"):
                continue
            offscreen.append((index, entry))

    # Largest first, ties broken by document order; then back into document
    # order for output, so the ranking picks the entries and the ordering
    # still reads top-to-bottom.
    visible.sort(key=lambda item: (
        -(item[1]["box"]["width"] * item[1]["box"]["height"]), item[0]
    ))
    chosen_visible = visible[:MAX_VISIBLE_ENTRIES]
    chosen_visible.sort(key=lambda item: item[0])
    for number, (_, entry) in enumerate(chosen_visible, start=1):
        entry["id"] = f"V{number}"

    # Nearest first: an offscreen control 200px away is one flick away and a
    # one 4000px away may not be worth the trip.
    offscreen.sort(key=lambda item: (item[1].get("dist", 0), item[0]))
    chosen_offscreen = offscreen[:MAX_OFFSCREEN_ENTRIES]
    chosen_offscreen.sort(key=lambda item: item[0])
    for number, (_, entry) in enumerate(chosen_offscreen, start=1):
        entry["id"] = f"O{number}"

    return {
        "display_width": display_w,
        "display_height": display_h,
        "visible": [entry for _, entry in chosen_visible],
        "offscreen": [entry for _, entry in chosen_offscreen],
    }


def find_entry(ui_map: Optional[Dict[str, Any]], element_id: str) -> Optional[Dict[str, Any]]:
    """The entry this id names in this map, or None.

    Only the map it was issued from answers: an id is a name for a position in
    one snapshot's list, and searching any other list for it is the
    reinterpretation this design refuses to do.
    """
    if not isinstance(ui_map, dict) or not element_id:
        return None
    for key in ("visible", "offscreen"):
        for entry in ui_map.get(key) or []:
            if isinstance(entry, dict) and entry.get("id") == element_id:
                return entry
    return None


def click_point(ui_map: Optional[Dict[str, Any]], element_id: str) -> Optional[Tuple[int, int]]:
    """The centre of this id's box, on the display grid, or None.

    The centre is the click point rather than the top-left or a point found by
    scanning: a control's centre is the one pixel that belongs to the control
    under any padding, icon or border, and the hit test that runs before the
    click is what decides whether that claim is true.
    """
    entry = find_entry(ui_map, element_id)
    if entry is None or not isinstance(entry.get("box"), dict):
        return None
    display_w = int((ui_map or {}).get("display_width") or 0) if isinstance(ui_map, dict) else 0
    display_h = int((ui_map or {}).get("display_height") or 0) if isinstance(ui_map, dict) else 0
    return box_point(entry["box"], display_w, display_h)


def box_point(box: Dict[str, int], display_w: int, display_h: int) -> Tuple[int, int]:
    """A box's centre as integers, clamped onto the display."""
    x = int(box.get("x") or 0)
    y = int(box.get("y") or 0)
    width = max(1, int(box.get("width") or 1))
    height = max(1, int(box.get("height") or 1))
    px = x + width // 2
    py = y + height // 2
    if display_w > 0:
        px = max(0, min(px, display_w - 1))
    if display_h > 0:
        py = max(0, min(py, display_h - 1))
    return (px, py)


def _offscreen_text(entry: Dict[str, Any]) -> str:
    """`below ~830px` and whether its own panel scrolls, for a line or a refusal."""
    parts: List[str] = []
    off = entry.get("off")
    if off:
        parts.append(str(off))
    dist = entry.get("dist")
    if isinstance(dist, int) and dist > 0:
        parts.append(f"~{dist}px")
    if entry.get("scroller"):
        parts.append("in a scrollable panel")
    return ", ".join(parts)


def format_ui_map(ui_map: Optional[Dict[str, Any]]) -> str:
    """The block the model reads, or "" when there is no map to read.

    The header carries the three facts that make the lines interpretable --
    that ids are the way to click, that an id belongs to this map only, and
    that boxes are in the screenshot's own pixels -- because a map without
    those is a list of numbers whose units the model has to guess.

    An empty page still earns a line: "no clickable elements" is a fact the
    model needs (click by coordinates), and silence would be indistinguishable
    from a failed read.
    """
    if not isinstance(ui_map, dict):
        return ""
    visible = ui_map.get("visible") or []
    offscreen = ui_map.get("offscreen") or []
    if not visible and not offscreen:
        return (
            "UI map (live DOM): no clickable elements; click by coordinates "
            "from the screenshot."
        )

    lines = [
        'UI map (live DOM): click with element_id (e.g. "V3"); an id belongs '
        "only to this map. Boxes are (x0,y0)-(x1,y1) in screenshot pixels. "
        "Offscreen entries have no box -- scroll first; the map is rebuilt "
        "after every scroll."
    ]
    for entry in visible:
        box = entry.get("box") or {}
        line = f'[{entry.get("id", "?")}] {_label(entry)}'
        display_name = entry.get("name") or entry.get("text")
        if display_name:
            line += f' "{display_name}"'
        line += (
            f' ({box.get("x", 0)},{box.get("y", 0)})-'
            f'({box.get("x", 0) + box.get("width", 0)},'
            f'{box.get("y", 0) + box.get("height", 0)})'
        )
        if entry.get("editable"):
            line += " editable"
        if entry.get("disabled"):
            line += " disabled"
        lines.append(line)
    for entry in offscreen:
        line = f'[{entry.get("id", "?")}] {_label(entry)}'
        display_name = entry.get("name") or entry.get("text")
        if display_name:
            line += f' "{display_name}"'
        line += " offscreen"
        where = _offscreen_text(entry)
        if where:
            line += f" {where}"
        lines.append(line)
    return "\n".join(lines)


def resolve_entry(
    sent_map: Optional[Dict[str, Any]],
    sent_page: str,
    fresh_map: Optional[Dict[str, Any]],
    fresh_page: str,
    element_id: str,
) -> Tuple[Optional[Tuple[int, int]], str]:
    """The point to click for this id, or the reason not to click it.

    Returns `(point, "")` to click and `(None, reason)` to refuse, where the
    reason is written for the model: what the id was, what became of it, and
    what to do instead.

    The order of the checks is the order of what can go wrong, cheapest first
    -- format, then page, then presence, then identity -- and every one of them
    fails closed.  The point that comes back is the centre of the *fresh*
    entry's box, not the one the request carried: the request's box is where
    the control was when the map was read, and a control that moved (a banner
    dismissed, a list re-sorted) is still the same control and still wants a
    click at its new centre.

    The identity comparison in the middle is what makes "same id" mean "same
    control": ids are never reused within a map, but between two reads of a
    page that re-rendered, `V3` can sit on a different control entirely, and
    role, name, tag, type and text changing under an id is the page telling us
    so.
    """
    if not ELEMENT_ID_RE.match(element_id or ""):
        return None, f'"{element_id}" is not a valid element id (expected e.g. "V3" or "O2")'

    # The page itself: two reads that are not the same page cannot be
    # reconciled entry by entry, and an unknown page is not a page we can
    # confirm anything about.
    if not sent_page or not fresh_page:
        return None, (
            f"{element_id} cannot be confirmed: the page could not be "
            "identified; click by coordinates from the screenshot instead"
        )
    if sent_page != fresh_page:
        return None, (
            f"{element_id} is stale: the page changed since the UI map was "
            "read; take a screenshot() and use a current id or coordinates"
        )

    sent = find_entry(sent_map, element_id)
    if sent is None:
        return None, (
            f"{element_id} is not in the UI map sent with this request; take "
            'a screenshot() and use a current id (e.g. "V3") or coordinates'
        )
    if "box" not in sent:
        where = _offscreen_text(sent)
        return None, (
            f"{element_id} is offscreen ({describe_entry(sent)}"
            f'{", " + where if where else ""}): scroll first, then the '
            "refreshed map will show it with a box"
        )

    fresh = find_entry(fresh_map, element_id)
    if fresh is None:
        return None, (
            f"{element_id} is no longer on the page: a fresh read of the UI "
            "map does not contain it; take a screenshot() and choose again"
        )
    if _identity(sent) != _identity(fresh):
        return None, (
            f"{element_id} now describes a different element "
            f"({describe_entry(sent)} -> {describe_entry(fresh)}): the page "
            "changed since the map was read; take a screenshot() and choose "
            "again"
        )
    if fresh.get("disabled"):
        return None, (
            f"{element_id} is disabled and cannot be clicked "
            f"({describe_entry(fresh)})"
        )
    if "box" not in fresh:
        return None, (
            f"{element_id} moved offscreen before the click "
            f"({describe_entry(fresh)}): scroll first, then use the refreshed "
            "map"
        )
    if not fresh["box"].get("width") or not fresh["box"].get("height"):
        return None, f"{element_id} has no usable box on the page right now"

    display_w = 0
    display_h = 0
    if isinstance(fresh_map, dict):
        display_w = int(fresh_map.get("display_width") or 0)
        display_h = int(fresh_map.get("display_height") or 0)
    return box_point(fresh["box"], display_w, display_h), ""


def resolution_reason(reason: str) -> str:
    """One refusal reason as the model-facing line.

    Every refusal from `resolve_entry` becomes the same sentence shape the
    coordinate path already uses -- `click was not performed: ...` -- so a
    model reading its own transcript sees one kind of failure, not two.
    """
    return f"click was not performed: {reason}"


__all__ = [
    "ELEMENT_ID_RE",
    "MAX_NAME_CHARS",
    "MAX_OFFSCREEN_ENTRIES",
    "MAX_VISIBLE_ENTRIES",
    "box_point",
    "build_ui_map",
    "click_point",
    "describe_entry",
    "find_entry",
    "format_ui_map",
    "resolution_reason",
    "resolve_entry",
]
