"""The `next_step` memory: what the model is told it planned, and when.

The failure these tests exist for is a loop that looks like progress: the model
clicks a textbox, asks for a fresh screenshot, is shown the same textbox, and
clicks it again.  Every piece of context the loop sent it was consistent with
that -- the picture still shows the control, `History.txt` says it clicked it, the
executor says the click worked -- and none of them said the click had already
*focused* the field, because that is not visible in a picture of a textbox.

So the model writes down what it means to do next, and the next request is sent
that.  What is asserted here is the timing, because a plan that arrives late is
worse than no plan: a stale one tells the model to repeat the click it already
performed, which is the loop itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
import unittest.mock
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.computer.controller import ComputerError  # noqa: E402
from app.computer.next_step import (  # noqa: E402
    NEXT_STEP_ARGUMENT,
    extract_next_step,
    format_next_step,
)
from app.computer.runner import ComputerRunner, STATUS_DONE  # noqa: E402
from app.computer.tools import TOOL_NAMES, computer_tools  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.providers.base import Done, ToolCall, ToolCallEvent  # noqa: E402
from app.providers.router import Router  # noqa: E402

SCREEN_WIDTH = 1365
SCREEN_HEIGHT = 768


class NextStepTestCase(unittest.TestCase):
    """Base case: the provider's global call pacing off.

    Every provider shares one ten-second gap between model calls, which is right in
    production and makes a five-request test take a minute.  It is patched per
    test rather than at import so a failure cannot leak a zero gap into another
    suite.
    """

    def setUp(self) -> None:
        import app.providers.base as provider_base

        self._gate_patch = unittest.mock.patch.object(
            provider_base, "MODEL_CALL_GAP_SECONDS", 0.0
        )
        self._gate_patch.start()
        self.addCleanup(self._gate_patch.stop)


class FakeProvider:
    """Replays a scripted list of native tool calls and records what it was sent.

    Only `stream()` is implemented, deliberately: a double that invents its own
    `chat()` can pass green against a call the production path cannot make.
    """

    def __init__(self, replies: List[Dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: List[List[Any]] = []
        self.name = "openrouter"
        self.api_key = "sk-or-test-key"
        self.last_wire: dict = {}
        self.last_usage: dict = {}

    async def stream(self, messages, tools, model):
        self.calls.append(list(messages))
        reply = (
            self.replies.pop(0)
            if self.replies
            else {"name": "done", "args": {"message": "end"}}
        )
        yield ToolCallEvent(
            ToolCall(id="call-1", name=reply["name"], arguments=json.dumps(reply["args"]))
        )
        yield Done()


class FakeComputer:
    """The machine, and the page it is on, with both answering truthfully."""

    def __init__(self) -> None:
        self.url = "https://example.test/home"
        self.focus: Optional[Dict[str, str]] = {"role": "textbox", "name": "Write a comment"}
        self.dialog = None
        self.selected = None
        self.screens = 0
        self.actions: List[tuple] = []
        #: Set to make the state read fail, the way a browser that will not
        #: answer CDP does.
        self.broken = False

    async def state(self):
        if self.broken:
            raise ComputerError("chrome is not answering on the debug port")
        return {
            "ok": True,
            "url": self.url,
            "focus": dict(self.focus) if self.focus else None,
            "dialog": self.dialog,
            "selected": self.selected,
        }

    async def navigate(self, url):
        self.actions.append(("navigate", url))
        self.url = url
        return {"ok": True}

    async def search(self, query):
        self.actions.append(("search", query))
        return {"ok": True}

    async def click(self, x, y, move=False):
        self.actions.append(("click", x, y))
        return {
            "ok": True,
            "x": int(x),
            "y": int(y),
            "landed": True,
            "display_width": SCREEN_WIDTH,
            "display_height": SCREEN_HEIGHT,
        }

    async def hit(self, x, y):
        # The click contract reads the page at the point before the press, so
        # the fixture has to answer what is under the cursor.  Whatever point
        # is asked about, the element here is the composer those runs click.
        return {
            "ok": True,
            "in_page": True,
            "display_width": SCREEN_WIDTH,
            "display_height": SCREEN_HEIGHT,
            "element": {"role": "textbox", "name": "the composer", "text": ""},
        }

    async def type_text(self, text):
        self.actions.append(("type", text))
        return {"ok": True}

    async def key(self, combo):
        self.actions.append(("key", combo))
        return {"ok": True}

    async def scroll(self, delta_y):
        self.actions.append(("scroll", delta_y))
        return {"ok": True}

    async def move(self, x, y):
        return {"ok": True, "x": int(x), "y": int(y), "landed": True,
                "display_width": SCREEN_WIDTH, "display_height": SCREEN_HEIGHT}

    async def screenshot(self):
        self.screens += 1
        return f"SCREENSHOT-{self.screens}", SCREEN_WIDTH, SCREEN_HEIGHT


def plan(tool: str, instruction: str, condition: str) -> Dict[str, Any]:
    """A well-formed plan, as a model would write it."""
    return {"tool": tool, "instruction": instruction, "condition": condition}


def call(name: str, args: Dict[str, Any], history: str, next_step: Any = "MISSING") -> Dict[str, Any]:
    """One scripted native call.  `next_step=None` means "sent, but unusable"."""
    payload = dict(args)
    payload["history"] = history
    if next_step != "MISSING":
        payload[NEXT_STEP_ARGUMENT] = next_step
    return {"name": name, "args": payload}


def make_runner(replies: List[Dict[str, Any]]):
    provider = FakeProvider(replies)
    settings = load_settings()
    settings.computer_max_steps = 8
    settings.computer_max_json_retries = 2
    settings.computer_settle_ms = 0
    settings.computer_settle_ms_click = 0
    settings.computer_model = "test/vision"
    settings.computer_provider = "openrouter"
    settings.workspace_base_url = "http://127.0.0.1:9"
    runner = ComputerRunner(settings, Router({"openrouter": provider}, settings), db=None)
    runner.computer = FakeComputer()
    return runner, provider


async def finish(runner: ComputerRunner, task: str):
    run = await runner.start(task)
    for _ in range(500):
        if run.status in (STATUS_DONE, "stopped", "error"):
            return run
        await asyncio.sleep(0.02)
    return run


def user_text(call_messages) -> str:
    return "\n".join(m.content or "" for m in call_messages if m.role == "user")


def planned_tool(user: str) -> Optional[str]:
    """The tool named by the previous plan in a request, or None.

    Read out of the rendered block rather than off the run, because the block is
    what the model is actually shown; a plan stored correctly and rendered wrongly
    would pass a check that only looked at the run.
    """
    marker = "Previous AI next step"
    if marker not in user:
        return None
    payload = user.split(marker, 1)[1]
    payload = payload[payload.index("{"):]
    return json.loads(payload[: payload.rindex("}") + 1])[NEXT_STEP_ARGUMENT]["tool"]


class NextStepTiming(NextStepTestCase):
    """The exact sequence from the failure: the plan always moves on."""

    def test_the_plan_the_next_request_receives_is_the_newest_one(self):
        # navigate -> screenshot -> click the composer -> (one more request)
        # The fourth request must be told "type", and must never still be told
        # "click the composer", which is the click that already succeeded.
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening the page.",
                 plan("screenshot", "Inspect the page to find the composer.", "Use the fresh frame.")),
            call("screenshot", {}, "Looking at the page.",
                 plan("click", "Activate the visible composer control.", "Coordinates from the latest frame only.")),
            call("click", {"x": 100, "y": 100}, "Clicking the composer.",
                 plan("type", "Type the requested text into the focused composer.",
                      "Only if the UI state shows the composer focused; otherwise re-inspect.")),
            call("type", {"text": "hello"}, "Typing the text.",
                 plan("screenshot", "Verify the text is in the composer.", "Judge from the new frame.")),
            call("done", {"message": "posted"}, "Finished."),
        ])
        run = asyncio.run(finish(runner, "post a comment"))
        self.assertEqual(run.status, STATUS_DONE)

        texts = [user_text(c) for c in provider.calls]
        self.assertGreaterEqual(len(texts), 4, "the scripted run did not get that far")

        self.assertIsNone(planned_tool(texts[0]), "the first request must carry no previous plan")
        self.assertEqual(planned_tool(texts[1]), "screenshot")
        self.assertEqual(planned_tool(texts[2]), "click")
        # The assertion the whole feature exists for.
        self.assertEqual(planned_tool(texts[3]), "type")
        self.assertNotIn("Activate the visible composer control", texts[3],
                         "the superseded plan was sent again")

        # And the same request carries the live page, so the plan is checkable.
        self.assertIn("Current UI state", texts[3])
        self.assertIn('Accessible name: "Write a comment"', texts[3])
        self.assertIn("Previous AI next step", texts[3])

    def test_only_the_latest_plan_is_sent_never_a_list(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening.",
                 plan("screenshot", "Step one.", "Verify one.")),
            call("screenshot", {}, "Looking.",
                 plan("click", "Step two.", "Verify two.")),
            call("click", {"x": 10, "y": 10}, "Clicking.",
                 plan("type", "Step three.", "Verify three.")),
            call("done", {"message": "ok"}, "Done.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        asyncio.run(finish(runner, "do a thing"))
        texts = [user_text(c) for c in provider.calls]
        self.assertEqual(texts[3].count("Previous AI next step"), 1)
        self.assertNotIn("Step two", texts[3])
        self.assertNotIn("Step one", texts[3])
        self.assertIn("Step three", texts[3])

    def test_a_screenshot_call_can_produce_a_plan(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening.",
                 plan("screenshot", "Look at the page.", "Read the frame.")),
            call("screenshot", {}, "Looking.",
                 plan("click", "Activate the control I can see.", "Coordinates from this frame.")),
            call("done", {"message": "ok"}, "Done.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        asyncio.run(finish(runner, "look around"))
        texts = [user_text(c) for c in provider.calls]
        self.assertEqual(planned_tool(texts[1]), "screenshot")
        self.assertEqual(planned_tool(texts[2]), "click")

    def test_a_missing_plan_is_not_invented_and_does_not_block_the_action(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening."),  # no next_step
            call("done", {"message": "ok"}, "Done.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        run = asyncio.run(finish(runner, "go"))
        self.assertEqual(run.status, STATUS_DONE)
        texts = [user_text(c) for c in provider.calls]
        self.assertNotIn("Previous AI next step", texts[1],
                         "a plan that was never sent must not appear on the next request")
        # The action still happened: a missing plan is a note about the run, not a
        # refusal of work the model got right.
        self.assertIn(("navigate", "https://example.test/home"), runner.computer.actions)
        turn = run.trace[0]
        self.assertTrue(turn.parse_ok)
        self.assertIn("next_step", turn.next_step_error)

    def test_a_refused_call_does_not_keep_an_old_plan_forever(self):
        # Three replies that carry no usable plan: each one must clear the last
        # good one, rather than leaving it to be read as current on request 4.
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening.",
                 plan("click", "The good plan.", "Verify it.")),
            call("click", {"x": 10, "y": 10}, "Clicking.", None),          # unusable
            call("type", {"text": "x"}, "Typing.", {"tool": "type"}),       # incomplete
            call("done", {"message": "ok"}, "Done.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        asyncio.run(finish(runner, "go"))
        texts = [user_text(c) for c in provider.calls]
        # Request 1 has no previous plan at all.  Request 2 legitimately carries
        # the plan reply 1 wrote -- that is the feature working.  The claim under
        # test starts at request 3: the two replies after it each carried no
        # usable plan, so the good one must be gone rather than still readable as
        # current.
        self.assertIsNone(planned_tool(texts[0]))
        self.assertEqual(planned_tool(texts[1]), "click")
        for text in texts[2:]:
            self.assertNotIn("The good plan.", text,
                             "a superseded plan survived a response that carried none")

    def test_a_screenshot_request_carries_the_plan(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening.",
                 plan("screenshot", "Look.", "Read it.")),
            call("screenshot", {}, "Looking.",
                 plan("click", "Activate.", "This frame.")),
            call("click", {"x": 10, "y": 10}, "Clicking.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        asyncio.run(finish(runner, "go"))
        texts = [user_text(c) for c in provider.calls]
        # Request 2 is answered by the `screenshot` call, so the frame it captures
        # rides on request 3 -- which must carry the plan that call wrote, or a
        # turn that can see the screen is a turn with no idea what it meant to do
        # on it.
        self.assertEqual(planned_tool(texts[1]), "screenshot")
        self.assertEqual(planned_tool(texts[2]), "click")
        self.assertIn("Latest screenshot", texts[2])
        self.assertIn("Activate.", texts[2])

    def test_a_refused_call_revises_the_plan_in_its_retry(self):
        # The click is out of bounds, so it is refused; the retry has to carry the
        # refusal *and* the plan that refusal produced, newest first.
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening.",
                 plan("screenshot", "Look.", "Read it.")),
            call("screenshot", {}, "Looking.",
                 plan("click", "Activate the control.", "Coordinates from this frame.")),
            call("click", {"x": 10, "y": 900}, "Clicking too low.",
                 plan("click", "Activate the control inside the frame.", "Coordinates inside 1365x768.")),
            call("click", {"x": 10, "y": 20}, "Clicking again.",
                 plan("type", "Type the text.", "Composer must be focused.")),
            call("done", {"message": "ok"}, "Done.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        asyncio.run(finish(runner, "go"))
        texts = [user_text(c) for c in provider.calls]
        retry = texts[3]
        self.assertIn("was refused", retry)
        self.assertEqual(planned_tool(retry), "click")
        self.assertIn("Coordinates inside 1365x768.", retry)
        # The refusal is read after the plan, because it is the more urgent fact.
        self.assertGreater(retry.index("Previous AI next step"), 0)
        self.assertGreater(retry.index("was refused"), retry.index("Previous AI next step"))

    def test_an_unreadable_page_does_not_disturb_the_plan(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening.",
                 plan("screenshot", "Look.", "Read it.")),
            call("done", {"message": "ok"}, "Done.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        runner.computer.broken = True
        run = asyncio.run(finish(runner, "go"))
        self.assertEqual(run.status, STATUS_DONE)
        texts = [user_text(c) for c in provider.calls]
        self.assertIn("Current UI state: unavailable", texts[1])
        self.assertEqual(planned_tool(texts[1]), "screenshot")


class NextStepReachesOnlyTheModel(NextStepTestCase):
    """The executor must never see either metadata field."""

    def test_the_command_the_executor_receives_has_neither_field(self):
        runner, _ = make_runner([
            call("navigate", {"url": "https://example.test/home"}, "Opening.",
                 plan("screenshot", "Look.", "Read it.")),
            call("screenshot", {}, "Looking.",
                 plan("click", "Activate.", "This frame.")),
            call("click", {"x": 40, "y": 50, "target": "the composer"}, "Clicking.",
                 plan("type", "Type it.", "Focused first.")),
            call("done", {"message": "ok"}, "Done.",
                 plan("none", "Terminal response; no further model action is required.", "This run is finished.")),
        ])
        run = asyncio.run(finish(runner, "go"))
        for turn in run.trace:
            if turn.command:
                self.assertNotIn("history", turn.command)
                self.assertNotIn(NEXT_STEP_ARGUMENT, turn.command)
        self.assertIn(("click", 40, 50), runner.computer.actions)

    def test_the_tool_command_itself_drops_the_metadata(self):
        from app.computer.commands import Bounds
        from app.computer.tools import tool_to_command

        command, error = tool_to_command(
            "click",
            {
                "x": 10,
                "y": 20,
                "target": "the Post button",
                "history": "I've clicked it.",
                NEXT_STEP_ARGUMENT: plan("type", "Type it.", "Focused first."),
            },
            Bounds(SCREEN_WIDTH, SCREEN_HEIGHT),
        )
        self.assertEqual(error, "")
        self.assertEqual(command.to_json(), {"type": "click", "x": 10, "y": 20})


class NextStepSchema(NextStepTestCase):
    """The argument is on every tool, required, and described."""

    def test_every_tool_requires_both_metadata_arguments(self):
        schemas = computer_tools()
        self.assertEqual([s["name"] for s in schemas], list(TOOL_NAMES))
        for schema in schemas:
            properties = schema["parameters"]["properties"]
            self.assertIn("history", properties)
            self.assertIn(NEXT_STEP_ARGUMENT, properties)
            self.assertIn("history", schema["parameters"]["required"])
            self.assertIn(NEXT_STEP_ARGUMENT, schema["parameters"]["required"])

    def test_the_plan_object_requires_its_three_fields(self):
        plan_schema = computer_tools()[0]["parameters"]["properties"][NEXT_STEP_ARGUMENT]
        self.assertEqual(plan_schema["type"], "object")
        self.assertEqual(sorted(plan_schema["required"]), ["condition", "instruction", "tool"])

    def test_it_is_metadata_and_not_a_tool(self):
        self.assertNotIn(NEXT_STEP_ARGUMENT, TOOL_NAMES)

    def test_the_prompt_asks_for_a_plan_on_every_call(self):
        from app.computer.prompt import build_prompt

        prompt = build_prompt()
        self.assertIn("next_step", prompt)
        self.assertIn("Every tool call MUST also include a next_step object", prompt)


class NextStepValidation(NextStepTestCase):
    """Refusals name the field and the reason, and nothing is invented."""

    def test_a_good_plan_reads(self):
        got, error = extract_next_step({NEXT_STEP_ARGUMENT: plan("type", "Type it.", "Focused first.")})
        self.assertEqual(error, "")
        self.assertEqual(got["tool"], "type")

    def test_terminal_calls_may_plan_none(self):
        got, error = extract_next_step({NEXT_STEP_ARGUMENT: plan(
            "none", "Terminal response; no further model action is required.", "This run is finished.")})
        self.assertEqual(error, "")
        self.assertEqual(got["tool"], "none")

    def test_a_missing_plan_is_an_error_not_a_default(self):
        got, error = extract_next_step({"history": "I've clicked it."})
        self.assertEqual(got, {})
        self.assertIn("not present", error)

    def test_each_mistake_is_named(self):
        cases = [
            (None, "not present"),
            ("type the text", "not an object"),
            ({"tool": "type"}, "instruction"),
            ({"tool": "type", "instruction": "", "condition": "c"}, "empty"),
            ({"tool": "sh", "instruction": "i", "condition": "c"}, "not one of"),
            ({"tool": "click", "instruction": "click (344,107) now", "condition": "c"}, "coordinates"),
            ({"tool": "click", "instruction": "click it", "condition": "x=344"}, "coordinates"),
            ({"tool": "type", "instruction": "type it and confirm success", "condition": "c"}, "success"),
            ({"tool": "type", "instruction": "type it", "condition": "c", "extra": 1}, "condition"),
            ({"tool": "x" * 60, "instruction": "i", "condition": "c"}, "characters"),
        ]
        for value, needle in cases:
            with self.subTest(value=value):
                got, error = extract_next_step({NEXT_STEP_ARGUMENT: value} if value is not None else {})
                self.assertEqual(got, {})
                self.assertIn(needle, error)

    def test_an_empty_plan_renders_nothing(self):
        self.assertEqual(format_next_step({}), "")
        self.assertEqual(format_next_step(None), "")
        self.assertEqual(format_next_step({"tool": "type"}), "")

    def test_a_rendered_plan_is_valid_json_the_model_wrote(self):
        block = format_next_step(plan("type", 'Type "hello" now.', "Focused first."))
        payload = block[block.index("{"):]
        parsed = json.loads(payload)
        self.assertEqual(parsed[NEXT_STEP_ARGUMENT]["tool"], "type")
        self.assertEqual(parsed[NEXT_STEP_ARGUMENT]["instruction"], 'Type "hello" now.')


if __name__ == "__main__":
    unittest.main()