"""The computer-control prompt.

Deliberately short.  The prompt is re-sent on every request of every run, so every
word in it is a recurring cost -- and the previous version of this file was a
hundred and fifty lines that said the same thing nine or ten times over.  What it
bought was nothing the tool schemas do not already say: each tool's own
description is where its arguments and its limits live, and the model reads those
every turn anyway.

What the prompt has to carry is what a schema cannot, and there is one more thing
to it now.  A schema says what a tool accepts.  It cannot say who decides what
happened -- and a model that has just been told its own `history` lines are the
run's memory will eventually narrate a navigation that timed out as one that
worked.  So the prompt states the division of authority explicitly: the model
decides what to do, the executor reports what occurred, and the executor's report
is the truth.
"""

from __future__ import annotations

#: The whole prompt.  Kept as one string with no format placeholders on purpose:
#: a templated prompt invites per-run interpolation of a screen size, and the
#: only screen size that matters now is the one attached to the screenshot the
#: model actually received -- which it can measure for itself.
COMPUTER_CONTROL_PROMPT = (
    "You operate a real remote browser using only these tools: screenshot(), "
    "navigate(url), search(query), click(x,y), type(text), key(key), "
    "scroll(delta_y), history(note), done(message), error(message). "
    "Call exactly one tool per reply, and always supply its required arguments: "
    "navigate() without a url is refused, not guessed. When an action is "
    "required, call the tool; never write prose instead of a tool call. "
    "The executor reports whether each action succeeded or failed and why, and "
    "that report is the truth about the machine -- believe it over your own "
    "memory of what you asked for, and never invent or assume the current URL, "
    "page contents or screen. Ask for a screenshot only when you need to look; "
    "each one is sent to you once and never repeated. "
    "history(note) is optional and is your own note, not a record of what "
    "happened: what actually happened is already reported to you each turn."
)

#: Added to the user turn when the model has taken a screenshot, and only then.
#:
#: The size has to be stated because a coordinate is only meaningful in the grid
#: the image was measured in, and this way the number travels with the image
#: instead of living in a prompt that is describing every screen size the model
#: might ever see.  Sent once per screenshot rather than once per turn.
SCREENSHOT_NOTE = (
    "Latest screenshot: {width}x{height} pixels, coordinates measured from its "
    "top-left corner. This is the only current image; older ones are gone. If you "
    "need the screen again, call screenshot()."
)

#: Asked for when a call was refused.  Names the refusal because a model told
#: only "that failed" repeats the same call; a model told what was wrong with it
#: changes it.
REFUSAL_NOTE = "Refused: {error}"

#: Stated once, after a refusal, so the model can recover in the same run.
#: Deliberately not appended to every request: it is a recovery aid, and sending
#: it unconditionally is paying for advice nobody asked for.
RETRY_NOTE = "Call one tool now."


def screenshot_note(width: int, height: int) -> str:
    return SCREENSHOT_NOTE.format(width=width, height=height)


def build_prompt() -> str:
    """The prompt. No arguments, because it does not vary.

    `width`/`height` used to be interpolated here for every turn.  That is what
    made the old prompt expensive, and it was also wrong in a way that mattered:
    it described the size of the screen as though the model had it, on turns
    where no image was attached at all.
    """
    return COMPUTER_CONTROL_PROMPT
