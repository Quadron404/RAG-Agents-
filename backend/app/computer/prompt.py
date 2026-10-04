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
COMPUTER_CONTROL_PROMPT = (
    "You control a real remote browser. Call exactly one native tool per reply. "
    "Every tool call MUST include a required \"history\" string in its arguments; "
    "that string is your semantic memory of the action you are issuing right now. "
    "Example: {\"x\":344,\"y\":107,\"history\":\"I've clicked the Post button.\"} "
    "Write the history yourself, as a short sentence naming the page or the control. "
    "Never include coordinates, raw tool arguments, executor internals, or whether "
    "the action succeeded; only the executor reports that, separately. Do not write "
    "a second JSON object, and do not add prose after the call. "
    "Use the History.txt in the user turn as context; do not echo it into tool "
    "arguments. When an action is required, use the tool instead of prose. "
    "The executor's result is the only truth about what was executed; never invent "
    "or assume state. A successful action is already completed. Do not repeat the "
    "same successful action just because the control is still visible in a later "
    "screenshot. After a successful action, advance to the next step of the task. "
    "Repeat an action only when the latest screenshot or executor result shows that "
    "the previous action did not produce the required state. "
    "For a simple single-action task, act once; the executor ends the "
    "task automatically after a successful action, so do not call stop afterward. "
    "Ask for screenshot() only when visual inspection is needed; each screenshot is "
    "sent once per requested view."
)

#: Added to the user turn immediately after `Last action:`, and only on a turn
#: that has one -- so it can only ever be read next to the executor's verdict on
#: the step the model just took.
#:
#: It is here because the prompt alone did not stop the loop this is about: a
#: successful click was repeated after each new screenshot, because the model read
#: the control it had already acted on as the next thing to do.  A rule stated in
#: the system prompt is restated next to the two artifacts the model has to
#: reconcile -- its own `History.txt` line and the executor's `Last action:` line
#: -- which is where the decision is actually made.  It does not touch what counts
#: as success or what a click does; it only tells the model what a success means.
PROGRESS_NOTE = (
    "Progress rule: The last successful action is already completed. Do not repeat "
    "it unless the current screenshot proves it did not achieve the required state. "
    "Continue with the next task step."
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
