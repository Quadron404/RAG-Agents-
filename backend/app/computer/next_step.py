"""The model's plan for its next turn.

`history.py` stores what the model says it just did.  This stores what it says it
will do next, and it is a different thing: one is a record of the past, the other
is a statement about a future turn that has not happened yet.

Why it exists.  A screenshot is a picture of a control, and a picture does not
change when the control has already been used.  A model that clicked a textbox,
asked for a fresh screenshot and is shown the same textbox has no way to tell
"still to do" from "already done" from the image alone -- so it clicks again, and
the run walks a loop that looks like progress.  The fix is not a louder
instruction not to do that; it is to let the model write down what it intended
to do next, and hand that back to it on the next request, so it continues its own
reasoning instead of starting over from the picture.

So `next_step` is metadata inside the one native tool call the model already has
to make, exactly like `history`, and never an eleventh tool.  A second call would
break the one-call-per-reply rule, and a tool would make the plan something the
executor records -- which is the distinction this design turns on: the executor's
log says what happened, the model's plan says what it intends.

Three properties keep it from becoming something the loop trusts:

- it is never executed.  Nothing reads it to decide an action; it is sent to the
  model as text, and the model still has to choose a tool from the live state;
- it is always the newest one.  Every response replaces it, including a response
  that carried no usable plan -- which clears it rather than leaving the previous
  turn's plan to be read as current;
- it is never invented.  A missing or unusable plan is recorded as an error and
  nothing else happens, because a fabricated "next step" is a claim about a turn
  that has not run.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

#: The name of the argument inside every native tool call.  Metadata, like
#: `history`: it is not a tool, it is never executed, and it is stripped from the
#: arguments before the command is built.
NEXT_STEP_ARGUMENT = "next_step"

#: The one value of `tool` that is not a tool.  It is what `done`, `stop` and
#: `error` plan, because those calls end the run and there is no next turn to
#: plan for.  A model that names a tool there is claiming work it is not doing.
NONE_TOOL = "none"

#: Bound on each field.  Sent on the next request and read there, so this is a
#: plan for one immediate action: a field approaching a paragraph is the model
#: writing its task out again, and it is paid for on every remaining request.
#: Longer than this is refused rather than clipped -- a clipped plan is not the
#: plan that was written, and silently shortening it is the same failure as
#: inventing one.
MAX_TOOL_CHARS = 40
MAX_INSTRUCTION_CHARS = 400
MAX_CONDITION_CHARS = 300

#: The plan is prose about a future turn, so the same three shapes are refused
#: that `history` refuses, for the same reason: they are the representations this
#: system exists to stop.
#:
#: - ``344,107`` / ``(344,107)``  a coordinate pair.  Coordinates are the one
#:   thing in a plan that is guaranteed to be stale by the time it is read: the
#:   next turn may be a different frame, and the plan outlives the image.  A
#:   target is named, not pointed at.
#: - ``x=344``  a coordinate written as a tool argument;
#: - ``success`` / ``failed``  the executor's verdict.  It is not the model's to
#:   assert, and a plan that claims the next step already worked is a claim about
#:   a turn that has not run.
_COORD_PAIR_RE = re.compile(r"\b\d{1,5}\s*,\s*\d{1,5}\b|\b[xy]\s*=\s*-?\d", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(success\w*|succeed\w*|fail\w*)\b", re.IGNORECASE)

#: Raw tool JSON pasted into a field.  The call itself says all of it, and a plan
#: that opens with a brace is the model echoing arguments back at itself.
_RAW_TOOL_RE = re.compile(r"^\s*[\{\[]|\"(?:name|arguments|next_step|parameters)\"\s*:", re.IGNORECASE)

#: Sent above the plan on the next request.  It has to say three things: where
#: this came from, that it is guidance rather than an instruction, and what to do
#: when it no longer matches reality.  Without the third the model treats a plan
#: the current state has overtaken as an instruction to obey, which is how a stale
#: plan becomes a wrong action.
NEXT_STEP_NOTE = (
    "Previous AI next step (from the immediately previous response). Use it as a "
    "guide for this turn, but verify it against the latest UI state, screenshot, "
    "Last action, and task. If it is no longer correct, revise it."
)

_FIELDS = ("tool", "instruction", "condition")
_LIMITS = {
    "tool": MAX_TOOL_CHARS,
    "instruction": MAX_INSTRUCTION_CHARS,
    "condition": MAX_CONDITION_CHARS,
}

#: One message for every way of not having a plan: the argument absent from the
#: call, and the argument present and empty are the same failure, so they get the
#: same sentence.  Two messages for one fault is how a reader starts wondering
#: which of them the run actually hit.
_ABSENT_ERROR = (
    f"the {NEXT_STEP_ARGUMENT} argument was not present; every native tool call must carry it"
)


def next_step_schema(description: str) -> Dict[str, Any]:
    """The `next_step` argument, added to every tool and required on every one.

    Built by a function rather than written out once so that a tool added later
    cannot ship without it by forgetting to copy the block, which is the same
    reason `history` goes through `_object_schema`.

    The three field descriptions are the shortest that still say what the field is
    for.  They are inside the schema of all ten tools, so they go out on every
    request of every turn, and the model is told what to put in them -- and what to
    do with the object afterwards -- by the system prompt and the parent block
    instead.  A field description is for the field, not for the feature.
    """
    return {
        "type": "object",
        "description": description,
        "properties": {
            "tool": {
                "type": "string",
                "description": "The tool the next turn should normally use.",
            },
            "instruction": {
                "type": "string",
                "description": "Exactly what the next step should accomplish.",
            },
            "condition": {
                "type": "string",
                "description": "What must be verified before doing it.",
            },
        },
        "required": list(_FIELDS),
    }


def _known_tools() -> frozenset:
    """The tools a plan may name.

    Imported lazily: `tools.py` builds its schemas from this module, so a
    module-level import here would be a cycle.
    """
    from .tools import TOOL_NAMES

    return frozenset(TOOL_NAMES)


def _clean(value: Any) -> str:
    """One field as a single line, or "" when there is nothing in it."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _validate(raw: Any) -> Tuple[Dict[str, str], str]:
    """One plan object, checked.  Returns ``(plan, error)``.

    Every rule lives here so that the tool-call path and any future fallback
    cannot disagree about what counts as a plan.  Refusals name the field and the
    reason, because "bad next_step" and "no next_step" call for different fixes.
    """
    if raw is None:
        return {}, _ABSENT_ERROR
    if not isinstance(raw, dict):
        return {}, f"the {NEXT_STEP_ARGUMENT} argument was {type(raw).__name__}, not an object"

    # Unknown fields are refused rather than ignored.  The three are the whole
    # plan, and a fourth is either the model rewriting the object it was given a
    # schema for or smuggling something past the checks below under a name they do
    # not look at -- so it is dropped here rather than filtered silently, and the
    # refusal names what a plan is made of so the model can correct it in one
    # reply.
    extra = sorted(set(raw) - set(_FIELDS))
    if extra:
        return {}, (
            f"the {NEXT_STEP_ARGUMENT} object has unexpected field(s) "
            f"{', '.join(repr(name) for name in extra)}; a plan has exactly "
            f"{', '.join(_FIELDS)}"
        )

    plan: Dict[str, str] = {}
    for name in _FIELDS:
        if name not in raw:
            return {}, f"the {NEXT_STEP_ARGUMENT} object has no {name!r} field"
        text = _clean(raw.get(name))
        if not text:
            return {}, f"the {NEXT_STEP_ARGUMENT} {name!r} field was empty"
        if len(text) > _LIMITS[name]:
            return {}, (
                f"the {NEXT_STEP_ARGUMENT} {name!r} field was {len(text)} characters; "
                f"a plan is one immediate step of at most {_LIMITS[name]}"
            )
        plan[name] = text

    allowed = _known_tools() | {NONE_TOOL}
    if plan["tool"] not in allowed:
        return {}, (
            f"the {NEXT_STEP_ARGUMENT} planned tool {plan['tool']!r} is not one of "
            f"{', '.join(sorted(allowed))}"
        )

    for name in ("instruction", "condition"):
        if _RAW_TOOL_RE.search(plan[name]):
            return {}, f"the {NEXT_STEP_ARGUMENT} {name!r} field was raw tool JSON, not a sentence"
        if _COORD_PAIR_RE.search(plan[name]):
            return {}, (
                f"the {NEXT_STEP_ARGUMENT} {name!r} field contained coordinates; a plan "
                f"is read after the screen may have changed, so it names its target "
                f"instead of pointing at it"
            )
        if _STATUS_RE.search(plan[name]):
            return {}, (
                f"the {NEXT_STEP_ARGUMENT} {name!r} field asserted an executor result; "
                f"only the executor reports success or failure, and the step it plans "
                f"has not run yet"
            )
    return plan, ""


def extract_next_step(args: Optional[Dict[str, Any]]) -> Tuple[Dict[str, str], str]:
    """The plan carried as an argument of the native tool call.

    This is the primary source and the only one: the plan exists to be read by
    the next request, and the tool call is the one place every provider this loop
    runs on fills.  Returns ``(plan, error)``; exactly one is meaningful, and an
    error never blocks the action -- the plan is context, and refusing a call over
    its plan would make the model unable to correct itself.
    """
    if not isinstance(args, dict):
        return {}, "the tool call arguments were not an object"
    # `.get` rather than a membership test, so the absent argument and the empty
    # one are reported by the same rule instead of two that can drift apart.
    return _validate(args.get(NEXT_STEP_ARGUMENT))


def format_next_step(plan: Optional[Dict[str, Any]]) -> str:
    """The plan as the next request reads it, or "" when there is none.

    Serialised through the JSON encoder rather than concatenated, so a quote or a
    brace in a field cannot produce a broken block in the next request.  The
    object is exactly the one the model wrote -- re-serialised, never rewritten --
    so what the next request shows is what the model actually planned.
    """
    if not isinstance(plan, dict) or not plan:
        return ""
    fields: Dict[str, str] = {}
    for name in _FIELDS:
        text = _clean(plan.get(name))
        if not text:
            return ""
        fields[name] = text
    payload = json.dumps({NEXT_STEP_ARGUMENT: fields}, ensure_ascii=False, indent=2)
    return f"{NEXT_STEP_NOTE}\n{payload}"


__all__ = [
    "MAX_CONDITION_CHARS",
    "MAX_INSTRUCTION_CHARS",
    "MAX_TOOL_CHARS",
    "NEXT_STEP_ARGUMENT",
    "NEXT_STEP_NOTE",
    "NONE_TOOL",
    "extract_next_step",
    "format_next_step",
    "next_step_schema",
]