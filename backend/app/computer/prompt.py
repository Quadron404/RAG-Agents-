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
is the truth.  It also carries the one pointer the schemas cannot: that a control
is named with an `element_id` from the UI map, and a coordinate is the fallback.
"""

from __future__ import annotations

#: The whole prompt.  Kept as one string with no format placeholders on purpose:
#: a templated prompt invites per-run interpolation of a screen size, and the
#: only screen size that matters now is the one attached to the screenshot the
#: model actually received -- which it can measure for itself.
#:
#: The response contract lives here because the model is the only party that can
#: satisfy it: `History.txt` is written from the model's own sentence about the
#: action it is issuing, so the words have to be its own.  The executor cannot
#: supply them, because it knows the coordinates and not the intent, and a memory
#: of `click (344,107) -> SUCCESS` tells the next request nothing it can act on.
#:
#: The sentence is requested as an argument of the tool call rather than as text
#: after it.  A native tool-calling reply has no text: these endpoints answer a
#: tool call with `content: null`, so there is nothing after the call to read and
#: nothing the model could be shown to put there.  One call, one `history`
#: argument, and the whole reply is the call -- which also keeps the
#: one-tool-call-per-reply rule intact by construction.
#:
#: Every behavioural rule the loop depends on is stated here once, in its
#: shortest true form.  The tool schemas carry the arguments and their limits,
#: so nothing here repeats them; the UI map carries the page's controls each
#: turn; what the prompt keeps is what a schema and a map cannot say -- whose
#: report is believed, when an action is complete, and that a control is named
#: with an element_id from the map rather than a coordinate out of a picture.
COMPUTER_CONTROL_PROMPT = (
    "You control a real remote browser. Call exactly one native tool per reply. "
    "Every tool call MUST include a required \"history\" string: one short "
    "sentence in your own words naming what you act on (e.g. \"I clicked the "
    "Post button.\"); never echo History.txt or include coordinates, arguments, "
    "internals, or success/failure -- it is the run's memory and only the "
    "executor reports that. "
    "Every tool call MUST also include a next_step object: your immediate next "
    "plan -- the next tool, what it must accomplish, and what to verify first; "
    "use \"tool\": \"none\" when done or stuck. It is guidance, not execution: "
    "continue from it next turn and revise it when the latest screenshot, UI "
    "map, UI state, Last action, or task disagrees. "
    "The executor's result is the only truth; never invent or assume state. A "
    "successful action is already done; repeat it only when the latest "
    "screenshot or result shows the desired state was not produced. When a "
    "field already holds the intended text (the UI state reports it) and a "
    "submit control is available, press that control instead of clicking the "
    "field again. When action is needed, call a tool, not prose. For a simple "
    "single-action task, act once: the executor ends the run after a success, "
    "so do not call stop afterward. "
    "screenshot() only when you must see the screen. A refused call means "
    "nothing was executed: reply with one corrected call, never the same one. "
    "Prefer element_id from the UI map; use x/y only when there is no usable "
    "element. Offscreen entries: scroll first, then the map shows them."
)

#: Added to the user turn when the model has taken a screenshot, and only then.
#:
#: The size has to be stated because a coordinate is only meaningful in the grid
#: the image was measured in, and this way the number travels with the image
#: instead of living in a prompt that is describing every screen size the model
#: might ever see.  Sent once per screenshot rather than once per turn.
#:
#: Kept to the three facts that are not recoverable from the image itself: its
#: size, where (0,0) is, and that a newer frame costs a call.  The longer wording
#: this replaced also said older images were dropped, which is true of every
#: request and true of this one -- restating it on the one request that carries an
#: image bought nothing.
SCREENSHOT_NOTE = (
    "Latest screenshot: {width}x{height}. Coordinates use the image's top-left as "
    "(0,0). Call screenshot() for a newer frame."
)

#: Asked for when a call was refused.  Carries the whole recovery instruction,
#: because this string is the only channel the refusal has.
#:
#: A model told only "that failed" repeats the same call; a model told what was
#: wrong with it changes it.  So the reason is quoted verbatim rather than
#: summarised -- it names the coordinate, the bound it broke and every other
#: specific the validator had -- and it is followed by the two things the model
#: must do with it: nothing was executed, so its picture of the screen is still
#: current, and the next reply has to be a different call.
#:
#: Sent on the user turn, which is where the screenshot it talks about is, and
#: only on a request that follows a refusal.  It used to be one word ("Refused:")
#: followed by a separate "Call one tool now." appended to whatever message
#: happened to be last -- which on a screenshot request was the tool result, so
#: the reason reached a model that could not see it and the tool result stopped
#: being one.
REFUSAL_NOTE = (
    "Your previous reply was refused and nothing was executed: {error}\n"
    "Reply now with one corrected tool call. Do not repeat the refused call."
)


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
