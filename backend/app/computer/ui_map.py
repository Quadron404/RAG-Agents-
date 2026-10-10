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

- Ids are per snapshot.  An id means "the entry this request's map showed",
  and it only ever resolves to the same control: the page's identity must
  match, then the exact id is tried, then the fresh entries are searched by
  identity (role, name, tag, type, text).  One unique match means the render
  merely renumbered the map and the control is still itself; no match means
  it is gone or replaced; several matches mean it is ambiguous -- all three
  refuse rather than point at a guess.
- Visible, offscreen and hidden are three different answers.  A visible entry
  carries a real box clipped to what is actually on screen; an offscreen entry
  carries no box at all (a coordinate for something not on the screen is a
  guess) but carries direction and distance so the model can scroll to it;
  a hidden entry -- display:none, visibility:hidden, zero-size -- is not
  rendered and therefore not mapped, because it cannot be clicked.
- The model never sees raw output.  The agent reports everything it found, in
  document order; this module decides what is worth sending (actionable
  controls only, the best of them ranked by name, editability, closeness to
  the viewport centre and area, duplicates collapsed, hard caps) and stamps
  ids only on that choice, so an id is always bounded and always means one of
  the entries the model was actually shown.
- Resolution re-reads the page instead of trusting the map.  The box in the
  request is where the control *was*; the click point is the centre of where
  it is now, with role, name, tag, type and text compared in between so a
  control that changed underneath the id is refused, not reinterpreted -- and
  a map that merely renumbered itself is re-found by identity rather than
  treated as gone.

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

#: How many visible entries the model is shown.  Twenty covers a page's real
#: controls while keeping the block smaller than the screenshot it sits next
#: to; the choice is ranked (see `build_ui_map`), so the things a person would
#: actually press first -- the primary button, the nav links -- are the ones
#: that survive.
MAX_VISIBLE_ENTRIES = 20

#: How many offscreen entries the model is shown, named nearest first.  They
#: cost more text than a visible entry (no box to point at) and are less
#: likely to be the next action, so they get the smaller budget.
MAX_OFFSCREEN_ENTRIES = 8

#: Longest name on one line.  A name is a label, not a paragraph: containers
#: fall back to their text content, and that can be the whole page.
MAX_NAME_CHARS = 36

#: Longest text carried for identity comparison.  Longer than the displayed
#: name on purpose: two controls can share a short label and differ in their
#: text, and identity is what decides whether a click is still safe.
MAX_TEXT_CHARS = 100

#: Roles a click could target.  Anything else -- containers, layout wrappers,
#: presentation, plain text -- is not a control, so it never earns a slot that
#: an actionable one would have taken.
ACTIONABLE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
    "switch", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
    "listbox", "option", "slider", "spinbutton", "treeitem",
})

#: Tags that are interactive even when the page gives them no role.  A bare
#: `<a href>` or `<input>` is clickable wherever it appears.
ACTIONABLE_TAGS = frozenset({
    "button", "a", "input", "select", "textarea", "summary", "option",
})

#: Roles the model types into.  Of two otherwise equal controls, the editable
#: one is the more likely next action, so it ranks ahead.
EDITOR_ROLES = frozenset({"textbox", "searchbox", "combobox"})


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


def same_control(left: Any, right: Any) -> bool:
    """Whether two element-shaped dicts describe the very same control.

    Both sides go through `_entry` first, which is the cleaning every map
    entry goes through, so a name the map cut short still equals the same name
    read whole by the hit test: the two readers report one element at
    different lengths, and a length is not a different control.

    This is the weaker of the two ways to say "the same control" -- the map's
    own DOM token, compared by the caller, is exact -- and it is the stand-in
    when no token is available.  Anything that cannot be described at all
    answers False, which a caller reads as *no opinion* rather than *no*.
    """
    ours = _entry(left) if isinstance(left, dict) else None
    theirs = _entry(right) if isinstance(right, dict) else None
    if ours is None or theirs is None:
        return False
    return _identity(ours) == _identity(theirs)


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


def _valid_point(raw: Any) -> Optional[Dict[str, int]]:
    """A verified click point as two ints, or None when it is unusable.

    The agent ships this on a visible entry: a display pixel the live page
    still resolves to that control -- its box centre in the common case, or
    another pixel inside the box when something paints over the centre.  It is
    internal (the model reads boxes) and it is not geometry the map trusts, so
    it is validated exactly as strictly as a box and simply dropped when it is
    malformed, leaving the box centre as the fallback.
    """
    if isinstance(raw, dict):
        raw = [raw.get("x"), raw.get("y")]
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        x = int(raw[0])
        y = int(raw[1])
    except (TypeError, ValueError):
        return None
    return {"x": x, "y": y}


def _clamp_point(point: Dict[str, int], display_w: int, display_h: int) -> Tuple[int, int]:
    """A point kept on the display the screenshot was taken of."""
    px = int(point.get("x") or 0)
    py = int(point.get("y") or 0)
    if display_w > 0:
        px = max(0, min(px, display_w - 1))
    if display_h > 0:
        py = max(0, min(py, display_h - 1))
    return (px, py)


def _clamp_box(box: Dict[str, int], display_w: int, display_h: int) -> Dict[str, int]:
    """Keep a box on the display the screenshot was taken of."""
    if display_w > 0:
        box["x"] = max(0, min(box["x"], display_w - 1))
        box["width"] = max(1, min(box["width"], display_w - box["x"]))
    if display_h > 0:
        box["y"] = max(0, min(box["y"], display_h - 1))
        box["height"] = max(1, min(box["height"], display_h - box["y"]))
    return box


def _is_actionable(entry: Dict[str, Any]) -> bool:
    """Whether a click could ever target this entry."""
    if entry.get("editable"):
        return True
    if entry.get("role") in ACTIONABLE_ROLES:
        return True
    if entry.get("tag") in ACTIONABLE_TAGS:
        return True
    return False


def _is_editor(entry: Dict[str, Any]) -> bool:
    """Whether this entry is a text field a model types into."""
    return entry.get("editable") or entry.get("role") in EDITOR_ROLES


def _viewport(raw: Any) -> Optional[Dict[str, int]]:
    """The screenshot viewport boxes are measured in, when the agent sent one."""
    if not isinstance(raw, dict):
        return None
    return _valid_box(raw.get("viewport"))


def _vertical_gap(entry: Dict[str, Any], viewport: Optional[Dict[str, int]]) -> int:
    """How far this entry's centre is from the viewport centre, in display px.

    Zero when no viewport was sent: nothing to measure against, so every entry
    is equally central.  Used only in ranking -- of two otherwise equal
    controls, the one nearest the middle of the screen is the one the user has
    in view and so the more likely next action.
    """
    if viewport is None or "box" not in entry:
        return 0
    box = entry["box"]
    cy = box["y"] + box["height"] // 2
    vy = viewport["y"] + viewport["height"] // 2
    return abs(cy - vy)


def _overlap(left_box: Dict[str, int], right_box: Dict[str, int]) -> bool:
    """Whether two boxes cover the same ground, for deduplication.

    Two controls that report the same role, name, editability and type and sit
    on overlapping pixels are usually the same control seen twice -- a labelled
    link wrapped in a button, an input duplicated by its `<label>` -- and the
    second copy costs a slot and a line with nothing to add.  `intersection *
    2 >= smaller area` accepts boxes that mostly coincide while letting two
    genuinely distinct controls that merely touch pass.
    """
    ix = max(0, min(left_box["x"] + left_box["width"], right_box["x"] + right_box["width"])
             - max(left_box["x"], right_box["x"]))
    iy = max(0, min(left_box["y"] + left_box["height"], right_box["y"] + right_box["height"])
             - max(left_box["y"], right_box["y"]))
    if ix <= 0 or iy <= 0:
        return False
    inter = ix * iy
    small = min(left_box["width"] * left_box["height"], right_box["width"] * right_box["height"])
    return inter * 2 >= small


def _dedupe_visible(ranked: List[Tuple[int, Dict[str, Any]]]) -> List[Tuple[int, Dict[str, Any]]]:
    """Drop duplicate representations, keeping the best-ranked of each."""
    result: List[Tuple[int, Dict[str, Any]]] = []
    seen: Dict[Tuple[str, str, bool, str], Dict[str, int]] = {}
    for item in ranked:
        _, entry = item
        key = (
            entry.get("role") or "",
            entry.get("name") or "",
            entry.get("editable"),
            entry.get("type") or "",
        )
        kept = seen.get(key)
        if kept is not None and _overlap(kept, entry["box"]):
            continue
        if kept is None:
            seen[key] = entry["box"]
        result.append(item)
    return result


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
    # The token the page's own DOM stamped on this element, when it minted one.
    # It is the map's strongest statement of identity -- two elements share a
    # token only when they are one element -- and it never reaches the model,
    # which reads names and boxes, not tokens.
    dom_id = raw.get("dom_id")
    if isinstance(dom_id, int) and not isinstance(dom_id, bool) and dom_id > 0:
        out["dom_id"] = dom_id
    box = _valid_box(raw.get("box"))
    if box is not None:
        out["box"] = box
        point = _valid_point(raw.get("point"))
        if point is not None:
            out["point"] = point
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
    module's decision -- actionable controls only, ranked by name,
    editability, closeness to the viewport centre and area, duplicate
    representations collapsed, nearest named offscreen controls first, hard
    caps on both, and the chosen ones re-sorted into document order so the map
    reads down the page the way the page does.

    Ids are stamped after selection: `V1..Vn` over the visible choice,
    `O1..Om` over the offscreen choice, so an id's number is also a rough
    statement of priority.  Disabled offscreen controls are dropped -- a
    control that is out of view and cannot be acted on is not worth one of the
    eight slots -- while disabled visible ones are kept and marked, because
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
    viewport = _viewport(raw.get("viewport"))
    if isinstance(raw_visible, list):
        for index, candidate in enumerate(raw_visible[:400]):
            entry = _entry(candidate)
            if entry is None or "box" not in entry:
                continue
            if not _is_actionable(entry):
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
            if not _is_actionable(entry):
                continue
            if entry.get("disabled"):
                continue
            offscreen.append((index, entry))

    # Best first: named over unnamed, a text field over other controls of the
    # same rank, the one nearest the middle of the screen over one out of view,
    # biggest over smallest -- then duplicate representations collapse and the
    # hard cap is applied.  The ranking picks the entries; the final
    # document-order sort makes the map read down the page the way the page
    # does.
    visible.sort(key=lambda item: (
        0 if item[1].get("name") else 1,
        0 if _is_editor(item[1]) else 1,
        _vertical_gap(item[1], viewport),
        -(item[1]["box"]["width"] * item[1]["box"]["height"]),
        item[0],
    ))
    visible = _dedupe_visible(visible)
    chosen_visible = visible[:MAX_VISIBLE_ENTRIES]
    chosen_visible.sort(key=lambda item: item[0])
    for number, (_, entry) in enumerate(chosen_visible, start=1):
        entry["id"] = f"V{number}"

    # Nearest first: an offscreen control 200px away is one flick away and a
    # one 4000px away may not be worth the trip.  Named controls rank ahead of
    # anonymous wrappers, which the model cannot say anything useful about.
    offscreen.sort(key=lambda item: (
        0 if item[1].get("name") else 1,
        item[1].get("dist", 0),
        item[0],
    ))
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
    """The point to click for this id, on the display grid, or None.

    The verified point the agent shipped is preferred when it is present --
    it is a pixel the live page resolved to this control when the box was
    read -- and the box centre is the fallback, both clamped to the display.
    """
    entry = find_entry(ui_map, element_id)
    if entry is None or not isinstance(entry.get("box"), dict):
        return None
    display_w = int((ui_map or {}).get("display_width") or 0) if isinstance(ui_map, dict) else 0
    display_h = int((ui_map or {}).get("display_height") or 0) if isinstance(ui_map, dict) else 0
    point = entry.get("point")
    if isinstance(point, dict):
        return _clamp_point(point, display_w, display_h)
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
    """`below ~830` and whether its own panel scrolls, for a line or a refusal."""
    parts: List[str] = []
    off = entry.get("off")
    if off:
        parts.append(str(off))
    dist = entry.get("dist")
    if isinstance(dist, int) and dist > 0:
        parts.append(f"~{dist}")
    if entry.get("scroller"):
        parts.append("[panel]")
    return " ".join(parts)


def format_ui_map(ui_map: Optional[Dict[str, Any]]) -> str:
    """The block the model reads, or "" when there is no map to read.

    The header carries the three facts that make the lines interpretable --
    that ids are the way to click, that an id is this map's only, and that
    boxes are in the screenshot's own pixels -- because a map without those is
    a list of numbers whose units the model has to guess.

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
        'UI map (live DOM): click with element_id (e.g. "V3"); an id is this '
        "map's only. Boxes are screenshot pixels; offscreen entries have no "
        "box -- scroll first; the map is rebuilt after every scroll."
    ]
    for entry in visible:
        box = entry.get("box") or {}
        line = f'[{entry.get("id", "?")}] {_label(entry)}'
        display_name = entry.get("name") or entry.get("text")
        if display_name:
            line += f' "{display_name}"'
        line += (
            f" {box.get('x', 0)},{box.get('y', 0)}-"
            f"{box.get('x', 0) + box.get('width', 0)},"
            f"{box.get('y', 0) + box.get('height', 0)}"
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
    fails closed.  The point that comes back is the *fresh* entry's own: the
    verified point the agent shipped with it when one is present -- a pixel the
    live page resolved to this control, so a control whose box centre is
    painted over is still clicked where a person would land -- and the centre
    of its fresh box otherwise.  Either way it is the fresh entry, not the one
    the request carried: the request's box is where the control was when the
    map was read, and a control that moved (a banner dismissed, a list
    re-sorted) is still the same control and still wants a click where it now
    is.

    The identity comparison in the middle is what makes "same id" mean "same
    control": ids are never reused within a map, but between two reads of a
    page that re-rendered, `V3` can sit on a different control entirely.  A
    re-render also renumbers the map, so an id that no longer names its
    control is re-found by identity (role, name, tag, type, text) wherever it
    now sits before anything is refused: one unique match means the render
    merely renumbered and the control is still itself, several means the page
    now shows duplicates and the target is ambiguous, and none means the
    control is gone -- or, when the exact id still exists but now names
    something else, replaced.  All three conclusions refuse rather than point
    at a guess.
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

    # The map re-read may have renumbered itself (a banner pushed the layout
    # down, a list re-sorted), so the id that named this control in the sent
    # map can sit elsewhere now.  Find the control by identity wherever it is
    # and require a unique match before committing to a point.
    matches: List[Dict[str, Any]] = []
    if isinstance(fresh_map, dict):
        for key in ("visible", "offscreen"):
            for entry in fresh_map.get(key) or []:
                if isinstance(entry, dict) and _identity(entry) == _identity(sent):
                    matches.append(entry)
    if len(matches) == 1:
        candidate = matches[0]
    elif len(matches) > 1:
        return None, (
            f"{element_id} is ambiguous: {len(matches)} controls on the page "
            "match it; take a screenshot() and use a current id or coordinates"
        )
    elif find_entry(fresh_map, element_id) is not None:
        # The exact id still exists but now sits on a different control: this
        # element is not gone, it has been replaced.
        fresh = find_entry(fresh_map, element_id)
        return None, (
            f"{element_id} now describes a different element "
            f"({describe_entry(sent)} -> {describe_entry(fresh)}): the page "
            "changed since the map was read; take a screenshot() and choose "
            "again"
        )
    else:
        return None, (
            f"{element_id} is no longer on the page: a fresh read of the UI "
            "map does not contain it; take a screenshot() and choose again"
        )
    if candidate.get("disabled"):
        return None, (
            f"{element_id} is disabled and cannot be clicked "
            f"({describe_entry(candidate)})"
        )
    if "box" not in candidate:
        return None, (
            f"{element_id} moved offscreen before the click "
            f"({describe_entry(candidate)}): scroll first, then use the "
            "refreshed map"
        )
    if not candidate["box"].get("width") or not candidate["box"].get("height"):
        return None, f"{element_id} has no usable box on the page right now"

    display_w = 0
    display_h = 0
    if isinstance(fresh_map, dict):
        display_w = int(fresh_map.get("display_width") or 0)
        display_h = int(fresh_map.get("display_height") or 0)
    point = candidate.get("point")
    if isinstance(point, dict):
        return _clamp_point(point, display_w, display_h), ""
    return box_point(candidate["box"], display_w, display_h), ""


def resolved_entry(
    sent_map: Optional[Dict[str, Any]],
    fresh_map: Optional[Dict[str, Any]],
    element_id: str,
) -> Optional[Dict[str, Any]]:
    """The fresh control an id resolved to, as `resolve_entry` resolved it.

    Resolution has already refused every id this cannot answer for -- an id
    that is not in the sent map, a page that changed, a control that is gone
    or ambiguous -- so by the time this is called the search is a lookup with
    exactly one answer: the unique fresh entry whose identity matches the sent
    one, which is the same entry the point came from.

    The caller uses it to check the point against something the map knows
    rather than only against the model's wording; None means "nothing to check
    against", which its caller treats as no opinion rather than as a refusal.
    """
    if not isinstance(fresh_map, dict):
        return None
    sent = find_entry(sent_map, element_id)
    if sent is None:
        return None
    found: Optional[Dict[str, Any]] = None
    for key in ("visible", "offscreen"):
        for entry in fresh_map.get(key) or []:
            if not isinstance(entry, dict) or _identity(entry) != _identity(sent):
                continue
            if found is not None:
                return None
            found = entry
    return found


def resolution_reason(reason: str) -> str:
    """One refusal reason as the model-facing line.

    Every refusal from `resolve_entry` becomes the same sentence shape the
    coordinate path already uses -- `click was not performed: ...` -- so a
    model reading its own transcript sees one kind of failure, not two.
    """
    return f"click was not performed: {reason}"


__all__ = [
    "ACTIONABLE_ROLES",
    "ACTIONABLE_TAGS",
    "EDITOR_ROLES",
    "ELEMENT_ID_RE",
    "MAX_NAME_CHARS",
    "MAX_OFFSCREEN_ENTRIES",
    "MAX_TEXT_CHARS",
    "MAX_VISIBLE_ENTRIES",
    "box_point",
    "build_ui_map",
    "click_point",
    "describe_entry",
    "find_entry",
    "format_ui_map",
    "resolution_reason",
    "resolve_entry",
    "resolved_entry",
    "same_control",
]
