"""The UI map: what the model is shown, the id it picks, and the point that runs.

The loop already had a screenshot (pixels) and a state block (the page's words
about itself), and the model still had to estimate a coordinate out of an image
-- the one thing a vision model is worst at.  The UI map answers "which control
is this" structurally instead: the live DOM's own boxes, ranked, capped and
stamped with per-snapshot ids (`V1`, `O1`), so the model names a control rather
than guessing where one lives.

Three claims are proved here, each for a different reason it could silently
break:

- **What the model sees is bounded and honest.**  Actionable controls only,
  ranked (named, editable, near the viewport centre), duplicates collapsed,
  nearest named offscreen first, hard caps, document order for reading, and
  nothing that cannot be clicked -- an entry the page cannot describe never
  reaches the map, and a failed read produces no map at all rather than a
  stale one.
- **An id is a name for one control, never a position.**  Resolution re-reads
  the page at click time and refuses -- every time, with a reason the model
  can act on -- an id whose page changed, whose entry is gone, whose control
  changed identity, which is offscreen, or which is disabled.  A fresh read
  that merely renumbered itself is re-found by identity and still clicked.
  The point that does come back is the centre of the *fresh* box, so a
  control that moved is still clicked where it now is.
- **The loop carries it end to end.**  A fake machine whose `uimap` answers
  like the agent's proves the map reaches the request, the id reaches the
  executor, the refusal reaches the model as `click was not performed: ...`,
  and a click by id works before any screenshot while the identical click by
  coordinate is still refused for having seen nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.computer.commands import parse_command  # noqa: E402
from app.computer.controller import ComputerError  # noqa: E402
from app.computer.next_step import NEXT_STEP_ARGUMENT  # noqa: E402
from app.computer.runner import ComputerRunner, STATUS_DONE  # noqa: E402
from app.computer.tools import tool_to_command  # noqa: E402
from app.computer.ui_map import (  # noqa: E402
    ELEMENT_ID_RE,
    MAX_OFFSCREEN_ENTRIES,
    MAX_VISIBLE_ENTRIES,
    build_ui_map,
    find_entry,
    format_ui_map,
    resolution_reason,
    resolve_entry,
)
from app.config import load_settings  # noqa: E402
from app.providers.base import Done, ToolCall, ToolCallEvent  # noqa: E402
from app.providers.router import Router  # noqa: E402

# The provider layer paces real model calls ten seconds apart.  This module's
# runs drive turn after turn through a fake provider with no rate to protect,
# so the gate is zeroed at import, the same way `test_computer_control` does it
# (the suites run as separate processes, so this cannot leak into another).
import app.providers.base as _provider_base  # noqa: E402

_provider_base.MODEL_CALL_GAP_SECONDS = 0.0

DISPLAY_W = 1280
DISPLAY_H = 800

#: What the fixture page reports at every point a click asks about.  Named to
#: agree with the scripted target "Send button" the way a real page's
#: accessible name agrees with how a person refers to it -- the hit test runs
#: before every click, element id or not, so it has to be answerable here too.
HIT_ELEMENT = {
    "role": "button",
    "name": "Send",
    "text": "Send a message",
    "context": "the composer",
    "box": {"x": 100, "y": 100, "width": 80, "height": 40},
}


def visible(role: str, name: str, x: int, y: int, width: int, height: int,
             **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "role": role,
        "name": name,
        "tag": str(extra.pop("tag", "button")),
        "box": {"x": x, "y": y, "width": width, "height": height},
    }
    entry.update(extra)
    return entry


def offscreen(role: str, name: str, dist: int, off: str = "below",
              **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "role": role,
        "name": name,
        "tag": str(extra.pop("tag", "a")),
        "off": off,
        "dist": dist,
    }
    entry.update(extra)
    return entry


def raw_map(visible_entries: List[Any], offscreen_entries: List[Any],
            *, ok: bool = True) -> Dict[str, Any]:
    return {
        "ok": ok,
        "display_width": DISPLAY_W,
        "display_height": DISPLAY_H,
        "visible": list(visible_entries),
        "offscreen": list(offscreen_entries),
    }


def default_raw() -> Dict[str, Any]:
    """The fixture page as the agent would report it.

    V1 is the Send button (document order), V2 the composer, O1 a link below
    the fold: one of each kind, so a test can say which entry it means by id.
    """
    return raw_map(
        [
            visible("button", "Send", 100, 100, 80, 40),
            visible("textbox", "Write a message", 100, 160, 300, 90,
                    tag="textarea", editable=True),
        ],
        [offscreen("link", "Terms", 830)],
    )


def sent_and_fresh() -> tuple:
    """A sent map, its page identity, and a fresh read of the same page."""
    sent = build_ui_map(default_raw())
    fresh = build_ui_map(default_raw())
    return sent, "page-identity", fresh, "page-identity"


class BuildTheMapTheModelSees(unittest.TestCase):
    """Selection, caps, ids and the block itself."""

    def test_ids_are_stamped_in_document_order_over_the_best_ranked_entries(self):
        # 45 visible entries whose areas grow with their index: the 20 kept are
        # the 20 largest, and after selection they are numbered top-to-bottom.
        entries = [
            visible("button", f"Item {i}", 10, 10 + i * 14, 10, i + 1)
            for i in range(45)
        ]
        ui_map = build_ui_map(raw_map(entries, []))

        kept = ui_map["visible"]
        self.assertEqual(len(kept), MAX_VISIBLE_ENTRIES)
        self.assertNotIn("Item 0", [e["name"] for e in kept])
        self.assertEqual(kept[0]["name"], "Item 25")
        self.assertEqual(kept[-1]["name"], "Item 44")
        self.assertEqual(
            [e["id"] for e in kept],
            [f"V{i}" for i in range(1, MAX_VISIBLE_ENTRIES + 1)],
        )
        # Document order for reading, even though selection was by rank.
        names = [e["name"] for e in kept]
        self.assertEqual(names, sorted(names, key=lambda n: int(n.split()[1])))

    def test_offscreen_is_named_nearest_first_capped_and_barely_interactive(self):
        entries = [
            offscreen("link", f"Far {i}", 5000 - i * 100, disabled=(i == 0))
            for i in range(25)
        ]
        entries.append(offscreen("button", "Also disabled", 10, disabled=True))
        # A nearer but unnamed link, and two merely structural wrappers: never
        # a slot worth taking from a control the model can name.
        entries.append(offscreen("link", "", 5))
        entries.append(offscreen("div", "", 10, tag="div"))
        entries.append(offscreen("article", "", 10, tag="article"))
        ui_map = build_ui_map(raw_map([], entries))

        kept = ui_map["offscreen"]
        self.assertEqual(len(kept), MAX_OFFSCREEN_ENTRIES)
        self.assertNotIn("Far 0", [e["name"] for e in kept])
        self.assertNotIn("Also disabled", [e["name"] for e in kept])
        # Structural wrappers never reach the map at all.
        self.assertEqual(
            [e["role"] for e in kept], ["link"] * MAX_OFFSCREEN_ENTRIES
        )
        # Named controls rank ahead of nearer anonymous ones: the eight kept
        # are the eight nearest named links, numbered in document order.
        self.assertEqual(
            [e["name"] for e in kept],
            [f"Far {i}" for i in range(17, 17 + MAX_OFFSCREEN_ENTRIES)],
        )
        # Numbered in document order among the chosen.
        self.assertEqual(
            [e["id"] for e in kept],
            [f"O{i}" for i in range(1, MAX_OFFSCREEN_ENTRIES + 1)],
        )
        self.assertNotIn("box", kept[0])

    def test_unreadable_and_undescribable_entries_never_reach_the_map(self):
        # A failed read is not a map of an empty page: it is no map at all.
        self.assertEqual(build_ui_map(raw_map([], [], ok=False)), {})
        self.assertEqual(build_ui_map(None), {})
        self.assertEqual(build_ui_map("not the agent's reply"), {})

        ui_map = build_ui_map(
            raw_map(
                [
                    "not an entry",
                    {},
                    {"role": "", "name": "", "tag": ""},
                    visible("button", "Kept", 1, 1, 10, 10),
                ],
                ["also not an entry"],
            )
        )
        self.assertEqual([e["name"] for e in ui_map["visible"]], ["Kept"])
        self.assertEqual(ui_map["offscreen"], [])

    def test_the_block_states_the_contract_itself(self):
        ui_map = build_ui_map(
            raw_map(
                [
                    visible("button", "Send", 100, 100, 80, 40),
                    visible("button", "Delete", 10, 10, 10, 10, disabled=True),
                    visible("textbox", "Write a message", 100, 160, 300, 90,
                            tag="textarea", editable=True),
                ],
                [offscreen("link", "Terms", 830)],
            )
        )
        text = format_ui_map(ui_map)

        self.assertIn("element_id", text)
        self.assertIn("screenshot pixels", text)
        self.assertIn("an id is this map's only", text)
        self.assertIn('[V1] button "Send" 100,100-180,140', text)
        self.assertIn("editable", text)
        self.assertIn("disabled", text)
        self.assertIn('[O1] link "Terms" below ~830', text)
        # Coordinates appear in the entry lines, never in the header.
        header = text.splitlines()[0]
        self.assertNotIn("(100,100)", header)

        empty = format_ui_map(build_ui_map(raw_map([], [])))
        self.assertIn("no clickable elements", empty)
        self.assertIn("click by coordinates", empty)
        self.assertEqual(format_ui_map(None), "")


class ResolveAnIdAgainstAFreshRead(unittest.TestCase):
    """Every refusal, and the one point that is allowed through."""

    def test_a_valid_id_resolves_to_the_centre_of_the_fresh_box(self):
        sent, page, _, _ = sent_and_fresh()
        moved = build_ui_map(
            raw_map(
                [
                    visible("button", "Send", 150, 120, 80, 40),
                    visible("textbox", "Write a message", 100, 160, 300, 90,
                            tag="textarea", editable=True),
                ],
                [offscreen("link", "Terms", 830)],
            )
        )
        point, reason = resolve_entry(sent, page, moved, page, "V1")

        self.assertEqual(reason, "")
        self.assertEqual(point, (190, 140))
        # The fresh box, not the one the request carried (which centred at 140,120).
        self.assertNotEqual(point, (140, 120))

    def test_a_malformed_or_unknown_id_is_refused(self):
        sent, page, fresh, _ = sent_and_fresh()

        point, reason = resolve_entry(sent, page, fresh, page, "3")
        self.assertIsNone(point)
        self.assertIn("not a valid element id", reason)

        point, reason = resolve_entry(sent, page, fresh, page, "Z1")
        self.assertIsNone(point)
        self.assertIn("not a valid element id", reason)

        point, reason = resolve_entry(sent, page, fresh, page, "V99")
        self.assertIsNone(point)
        self.assertIn("not in the UI map sent with this request", reason)

        self.assertTrue(ELEMENT_ID_RE.match("V1"))
        self.assertTrue(ELEMENT_ID_RE.match("O20"))
        self.assertFalse(ELEMENT_ID_RE.match("V01x"))
        self.assertFalse(ELEMENT_ID_RE.match("1"))

    def test_a_page_that_changed_refuses_before_anything_else(self):
        sent, page, fresh, _ = sent_and_fresh()
        point, reason = resolve_entry(sent, page, fresh, "a-different-page", "V1")

        self.assertIsNone(point)
        self.assertIn("stale", reason)
        self.assertIn("page changed", reason)

        # An unidentified page is not a page anything can be confirmed on.
        point, reason = resolve_entry(sent, "", fresh, "", "V1")
        self.assertIsNone(point)
        self.assertIn("could not be identified", reason)

    def test_an_offscreen_id_says_where_it_is_and_asks_for_a_scroll(self):
        sent, page, fresh, _ = sent_and_fresh()
        point, reason = resolve_entry(sent, page, fresh, page, "O1")

        self.assertIsNone(point)
        self.assertIn("offscreen", reason)
        self.assertIn('link "Terms"', reason)
        self.assertIn("below", reason)
        self.assertIn("~830", reason)
        self.assertIn("scroll first", reason)

    def test_a_disabled_control_is_refused(self):
        sent, page, _, _ = sent_and_fresh()
        fresh = build_ui_map(
            raw_map(
                [
                    visible("button", "Send", 100, 100, 80, 40, disabled=True),
                    visible("textbox", "Write a message", 100, 160, 300, 90,
                            tag="textarea", editable=True),
                ],
                [offscreen("link", "Terms", 830)],
            )
        )
        point, reason = resolve_entry(sent, page, fresh, page, "V1")

        self.assertIsNone(point)
        self.assertIn("disabled", reason)

    def test_a_control_that_disappeared_is_refused(self):
        # Ids renumber per snapshot, so "gone" is what the fresh map says when
        # it holds fewer controls than the id's number: V2 was the composer,
        # and a fresh read of a page that now shows only one control has no V2
        # at all -- whichever control has taken V1's place.
        sent, page, _, _ = sent_and_fresh()
        fresh = build_ui_map(
            raw_map([visible("button", "Send", 100, 100, 80, 40)], [])
        )
        point, reason = resolve_entry(sent, page, fresh, page, "V2")

        self.assertIsNone(point)
        self.assertIn("no longer on the page", reason)

    def test_an_id_that_now_describes_a_different_control_is_refused(self):
        # Same id, same page, different control underneath it: the page
        # re-rendered between the map and the click, and "V1" is now a link.
        sent, page, _, _ = sent_and_fresh()
        fresh = build_ui_map(
            raw_map(
                [
                    visible("link", "Sponsored post", 100, 100, 80, 40, tag="a"),
                    visible("textbox", "Write a message", 100, 160, 300, 90,
                            tag="textarea", editable=True),
                ],
                [offscreen("link", "Terms", 830)],
            )
        )
        point, reason = resolve_entry(sent, page, fresh, page, "V1")

        self.assertIsNone(point)
        self.assertIn("different element", reason)
        self.assertIn('button "Send"', reason)

    def test_a_map_that_renumbered_itself_is_re_found_by_identity(self):
        # The same page re-rendered: a new control pushed the numbering down,
        # so the id the request carried (V1 = Send) now sits on the new
        # control.  The Send button is re-found by identity and clicked at its
        # fresh box -- the id was a name for a control, never a position.
        sent, page, _, _ = sent_and_fresh()
        fresh = build_ui_map(
            raw_map(
                [
                    visible("link", "Logo", 10, 10, 40, 20, tag="a"),
                    visible("button", "Send", 400, 300, 80, 40),
                    visible("textbox", "Write a message", 100, 160, 300, 90,
                            tag="textarea", editable=True),
                ],
                [offscreen("link", "Terms", 830)],
            )
        )
        point, reason = resolve_entry(sent, page, fresh, page, "V1")

        self.assertEqual(reason, "")
        self.assertEqual(point, (440, 320))
        # The fresh box, not the one the request carried at 140,120.
        self.assertNotEqual(point, (140, 120))

    def test_an_id_whose_control_is_now_duplicated_is_ambiguous(self):
        # Identity searching is only safe when the match is unique: two "Send"
        # buttons now exist, so neither can be claimed as the same control.
        sent, page, _, _ = sent_and_fresh()
        fresh = build_ui_map(
            raw_map(
                [
                    visible("button", "Send", 100, 100, 80, 40),
                    visible("button", "Send", 400, 100, 80, 40),
                    visible("textbox", "Write a message", 100, 160, 300, 90,
                            tag="textarea", editable=True),
                ],
                [offscreen("link", "Terms", 830)],
            )
        )
        point, reason = resolve_entry(sent, page, fresh, page, "V1")

        self.assertIsNone(point)
        self.assertIn("ambiguous", reason)
        self.assertIn("2 controls", reason)

    def test_every_refusal_reads_as_one_kind_of_failure(self):
        self.assertEqual(
            resolution_reason("V3 is stale"),
            "click was not performed: V3 is stale",
        )


class TheTwoParsersAgree(unittest.TestCase):
    """`tool_to_command` and `parse_command` accept and refuse the same replies.

    The loop reads native tool calls; the panel's JSON path reads prose
    replies.  Two validators that disagree would let the same click through
    one door and out the other, and a model told two different things about
    one mistake reads the difference as a new problem.
    """

    def test_an_element_click_parses_the_same_way_in_both(self):
        from app.computer.commands import Bounds

        args = {
            "element_id": "V3",
            "target": "Post button",
            "history": "I clicked Post.",
        }
        from_tool, tool_error = tool_to_command("click", args, None)
        from_parse, parse_error = parse_command(
            json.dumps(
                {"type": "click", "element_id": "V3", "target": "Post button"}
            ),
            bounds=Bounds(width=DISPLAY_W, height=DISPLAY_H),
        )

        self.assertEqual(tool_error, "")
        self.assertEqual(parse_error, "")
        for command in (from_tool, from_parse):
            self.assertEqual(command.type, "click")
            self.assertEqual(command.element_id, "V3")
            self.assertEqual(command.target, "Post button")
            # No coordinate exists yet: the point comes from the fresh read.
            self.assertEqual((command.x, command.y), (0.0, 0.0))

    def test_both_doors_refuse_the_same_bad_replies(self):
        cases = [
            ({"element_id": 7, "target": "Post button"},
             'element_id must be a string'),
            ({"element_id": "post-it", "target": "Post button"},
             "is not a UI map id"),
            ({"element_id": "V1"},
             'requires "target"'),
            ({"element_id": "V1", "target": "   "},
             'requires a non-empty "target"'),
        ]
        for args, fragment in cases:
            with self.subTest(args=args):
                _command, tool_error = tool_to_command("click", dict(args), None)
                self.assertIn(fragment, tool_error)

                _command, parse_error = parse_command(
                    json.dumps({"type": "click", **args}),
                    bounds=None,
                )
                self.assertIn(fragment, parse_error)


class FakeProvider:
    """Replays scripted native tool calls, one per reply, like a real endpoint."""

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


class PageComputer:
    """The machine, the page and the agent's DOM read, all answering truthfully.

    Unlike the other doubles, this one answers `uimap` the way the agent's
    `/computer/uimap` route does, because the claims under test are about the
    map travelling: request in, refusal out, point at the machine.
    """

    def __init__(self) -> None:
        self.url = "https://example.test/page"
        self.actions: List[tuple] = []
        self.hits: List[tuple] = []
        self.moves: List[tuple] = []
        self.screens = 0
        self.raw: Dict[str, Any] = default_raw()
        self.uimap_reads = 0
        self.uimap_broken = False
        self.hit_element: Dict[str, Any] = dict(HIT_ELEMENT)

    async def state(self):
        return {"ok": True, "url": self.url, "focus": None, "dialog": None}

    async def uimap(self):
        self.uimap_reads += 1
        if self.uimap_broken:
            raise ComputerError("the browser closed the debug port")
        return json.loads(json.dumps(self.raw))

    async def navigate(self, url):
        self.actions.append(("navigate", url))
        self.url = url
        return {"ok": True}

    async def search(self, query):
        self.actions.append(("search", query))
        return {"ok": True}

    async def hit(self, x, y):
        self.hits.append((int(x), int(y)))
        return {
            "ok": True,
            "in_page": True,
            "display_width": DISPLAY_W,
            "display_height": DISPLAY_H,
            "display_pixel": {"x": int(x), "y": int(y)},
            "window_rect": None,
            "element": dict(self.hit_element),
        }

    async def move(self, x, y):
        self.moves.append((int(x), int(y)))
        return {
            "ok": True,
            "x": int(x), "y": int(y),
            "actual_x": int(x), "actual_y": int(y),
            "landed": True,
            "display_width": DISPLAY_W,
            "display_height": DISPLAY_H,
        }

    async def click(self, x, y, move=True):
        self.actions.append(("click", int(x), int(y)))
        return {
            "ok": True,
            "x": int(x), "y": int(y),
            "actual_x": int(x), "actual_y": int(y),
            "landed": True,
            "display_width": DISPLAY_W,
            "display_height": DISPLAY_H,
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

    async def screenshot(self):
        self.screens += 1
        return f"SCREENSHOT-{self.screens}", DISPLAY_W, DISPLAY_H


def plan(tool: str, instruction: str, condition: str) -> Dict[str, Any]:
    return {"tool": tool, "instruction": instruction, "condition": condition}


def call(name: str, args: Dict[str, Any], history: str,
         next_step: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = dict(args)
    payload["history"] = history
    payload[NEXT_STEP_ARGUMENT] = next_step or plan(
        "none", "Read the next request and continue.", "The request arrives."
    )
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
    runner.computer = PageComputer()
    return runner, provider


async def finish(runner: ComputerRunner, task: str):
    run = await runner.start(task)
    for _ in range(500):
        if run.status in (STATUS_DONE, "stopped", "error"):
            return run
        await asyncio.sleep(0.02)
    return run


def user_text(messages) -> str:
    return "\n".join(m.content or "" for m in messages if m.role == "user")


#: Not a simple one-action task on purpose: `_simple_single_action` narrows the
#: tool catalogue for requests that begin "click ...", and these runs need the
#: whole catalogue to script a navigate before the click they are about.
TASK = "Open the page, press Send, then confirm the result."


class AnElementIdReachesTheMachine(unittest.TestCase):
    """The whole path: request carries the map, executor resolves the id."""

    def test_the_id_resolves_from_a_fresh_read_and_the_click_lands(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/page"}, "I opened the page."),
            call("click", {"element_id": "V1", "target": "Send button"},
                 "I clicked Send."),
            call("done", {"message": "finished"}, "The task is finished."),
        ])
        run = asyncio.run(finish(runner, TASK))

        # The map was read for the request the id was chosen from, and again
        # at click time: the point comes from a fresh read, never from the
        # request's box.
        self.assertGreaterEqual(runner.computer.uimap_reads, 2)
        self.assertIn("UI map (live DOM)", user_text(provider.calls[1]))
        self.assertIn('[V1] button "Send" 100,100-180,140',
                      user_text(provider.calls[1]))

        # Resolved centre of V1's box, then the four phases, then the press.
        self.assertEqual(runner.computer.moves, [(140, 120)])
        self.assertEqual(runner.computer.actions[-1], ("click", 140, 120))

        click_turn = run.trace[1]
        self.assertEqual(click_turn.command, {"type": "click", "element_id": "V1"})
        execution = click_turn.execution
        self.assertEqual(execution["outcome"], "executed")
        self.assertTrue(execution["executed"])
        self.assertEqual(execution["click_element_id"], "V1")
        # The resolved coordinate is what the execution record carries, while
        # the parse record stayed the id the model actually sent.
        self.assertEqual(execution["command"], {"type": "click", "x": 140.0, "y": 120.0})
        self.assertEqual(execution["x"], 140)
        self.assertEqual(execution["y"], 120)
        self.assertTrue(execution["click_target_verified"])

    def test_an_id_the_request_never_showcased_is_refused_without_moving(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/page"}, "I opened the page."),
            call("click", {"element_id": "V9", "target": "Send button"},
                 "I clicked the ninth control."),
            call("done", {"message": "finished"}, "The task is finished."),
        ])
        run = asyncio.run(finish(runner, TASK))

        # Nothing reached the machine: no pointer movement, no button event.
        self.assertEqual(runner.computer.moves, [])
        self.assertFalse(any(a[0] == "click" for a in runner.computer.actions))

        click_turn = run.trace[1]
        self.assertEqual(click_turn.command, {"type": "click", "element_id": "V9"})
        execution = click_turn.execution
        self.assertEqual(execution["outcome"], "refused")
        self.assertFalse(execution["executed"])
        self.assertTrue(execution["error"].startswith("click was not performed:"))
        self.assertIn("V9 is not in the UI map sent with this request",
                      execution["error"])
        self.assertEqual(execution["click_element_id"], "V9")

        # The model is told, in the same shape as any other refused click, and
        # its own history line never entered the run's memory.
        third_request = user_text(provider.calls[2])
        self.assertIn("click was not performed", third_request)
        self.assertNotIn("I clicked the ninth control", third_request)

    def test_an_id_is_refused_when_the_map_read_itself_failed(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/page"}, "I opened the page."),
            call("click", {"element_id": "V1", "target": "Send button"},
                 "I clicked Send."),
            call("done", {"message": "finished"}, "The task is finished."),
        ])
        runner.computer.uimap_broken = True
        run = asyncio.run(finish(runner, TASK))

        # A read that failed means no map on the request -- not a stale one.
        self.assertNotIn("UI map (live DOM)", user_text(provider.calls[1]))
        self.assertFalse(any(a[0] == "click" for a in runner.computer.actions))

        execution = run.trace[1].execution
        self.assertEqual(execution["outcome"], "refused")
        self.assertIn("no UI map is available on this request", execution["error"])
        self.assertIn("screenshot()", execution["error"])

    def test_a_click_by_id_needs_no_screenshot_while_a_coordinate_still_does(self):
        runner, provider = make_runner([
            call("navigate", {"url": "https://example.test/page"}, "I opened the page."),
            # Coordinate first: refused for having seen nothing, exactly as
            # before, because a coordinate guessed out of no picture lands on
            # whatever occupies that pixel.
            call("click", {"x": 140, "y": 120, "target": "Send button"},
                 "I clicked Send."),
            # The id, from the map the very same request was offered: allowed,
            # because it carries no coordinate to be wrong about.
            call("click", {"element_id": "V1", "target": "Send button"},
                 "I clicked Send."),
            call("done", {"message": "finished"}, "The task is finished."),
        ])
        run = asyncio.run(finish(runner, TASK))

        gate_refusal = user_text(provider.calls[2])
        self.assertIn("you have not seen the screen yet", gate_refusal)
        self.assertEqual(run.trace[1].tool_error,
                         "you have not seen the screen yet; call screenshot() and read "
                         "it before clicking")

        # Exactly one press, at the resolved point -- the refused coordinate
        # never moved the pointer.
        self.assertEqual(runner.computer.moves, [(140, 120)])
        self.assertEqual(runner.computer.actions[-1], ("click", 140, 120))

        element_turn = run.trace[2]
        self.assertEqual(element_turn.command, {"type": "click", "element_id": "V1"})
        self.assertEqual(element_turn.execution["outcome"], "executed")


if __name__ == "__main__":
    unittest.main()
