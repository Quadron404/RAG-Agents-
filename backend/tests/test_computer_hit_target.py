"""The click-target check: when the model's words and the point disagree.

`target_mismatch` is the one decision that decides whether a click happens at
all, so it is tested without a browser.  The failure this file exists for is a
click whose target named the *kind* of control it wanted -- "Send button" -- and
which was accepted because the point happened to contain the matching word
"Send", even though what was under the pointer was a link.  The disagreement is
about what the control *is*, not about a label more text could settle, so it is
a refusal; the text-field family is the documented exception, because
"textbox" landing on "textarea" is one control described two ways.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.computer.hit_target import target_mismatch  # noqa: E402


def hit(element: dict) -> dict:
    """A page answer holding one element, as the agent reports it."""
    return {"ok": True, "in_page": True, "element": element}


class KindConflict(unittest.TestCase):
    def test_a_button_target_over_a_link_is_refused_even_when_the_name_matches(self):
        reason = target_mismatch(
            "Send button",
            hit({"role": "link", "name": "Send", "tag": "a", "text": "Send"}),
            10,
            20,
        )
        self.assertNotEqual(reason, "")
        self.assertIn("Send button", reason)

    def test_a_checkbox_target_over_a_link_is_refused(self):
        reason = target_mismatch(
            "Remember me checkbox",
            hit({"role": "link", "name": "Remember me", "tag": "a",
                 "text": "Remember me"}),
            10,
            20,
        )
        self.assertNotEqual(reason, "")

    def test_a_button_target_over_a_button_still_passes(self):
        reason = target_mismatch(
            "Send button",
            hit({"role": "button", "name": "Send", "tag": "button", "text": "Send"}),
            10,
            20,
        )
        self.assertEqual(reason, "")


class FieldFamilyIsOneControl(unittest.TestCase):
    def test_a_textbox_target_over_a_textarea_is_not_a_kind_conflict(self):
        reason = target_mismatch(
            "Search textbox",
            hit({"role": "textarea", "name": "Search", "tag": "textarea"}),
            10,
            20,
        )
        self.assertEqual(reason, "")


class TheDomIsTheFact(unittest.TestCase):
    def test_the_resolved_token_still_settles_identity_before_the_words(self):
        # The id named the control and the point holds that very token, so the
        # click runs however the model's own wording described it.
        reason = target_mismatch(
            "Send button",
            hit({"role": "link", "name": "Send", "tag": "a", "dom_id": 42}),
            10,
            20,
            expected={"role": "link", "name": "Send", "tag": "a", "dom_id": 42},
        )
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
