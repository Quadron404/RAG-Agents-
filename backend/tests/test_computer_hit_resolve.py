"""The map's point-verification rule, exercised directly under `node`.

`resolvesTo` is the one decision that makes a UI-map point worth shipping: does
the live page, at this pixel, still resolve to this very control?  Get it too
eager and the map hands the click guard a point over a wrapping container --
the live failure was a Post button verified against a pixel that actually held
the dialog `div` around it -- which the guard then refuses, burning a whole
request.  Get it too strict and a point that lands on a glyph inside the
button is thrown away.  It is worth testing on its own, and it is pure over a
small facts object, so it can be tested without a browser at all.

The predicate lives in `vm_agent.daemon` as a JS string (`_RESOLVES_TO_JS`),
inlined into the map script at import.  This test hands that exact string to
`node` rather than re-implementing it in Python, because a Python copy would
happily pass while the real predicate drifted.  When `node` is not installed
the rule cannot be executed and the test skips rather than guessing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_agent import daemon  # noqa: E402


def _node() -> str | None:
    return shutil.which("node")


@unittest.skipUnless(_node(), "node is required to run the resolves-to predicate")
class TestResolvesToPredicate(unittest.TestCase):
    def test_accepts_the_control_and_rejects_a_container(self) -> None:
        scenarios = [
            {"name": "the page did not answer", "facts": {"found": False}, "want": False},
            {"name": "no node and no fields", "facts": {}, "want": False},
            {
                "name": "the pixel holds the control itself",
                "facts": {"found": True, "chosenIsEl": True},
                "want": True,
            },
            {
                "name": "the pixel holds a glyph inside the control",
                "facts": {"found": True, "elContainsChosen": True},
                "want": True,
            },
            {
                "name": "the pixel holds the control directly",
                "facts": {"found": True, "nodeIsEl": True},
                "want": True,
            },
            {
                "name": "the pixel holds nested markup inside the control",
                "facts": {"found": True, "elContainsNode": True},
                "want": True,
            },
            {
                "name": "the pixel holds an ancestor of the control",
                "facts": {"found": True, "chosenContainsEl": True},
                "want": False,
            },
            {
                "name": "the pixel holds a container above the control",
                "facts": {"found": True, "nodeContainsEl": True},
                "want": False,
            },
            {
                "name": "the pixel holds something else entirely",
                "facts": {"found": True},
                "want": False,
            },
        ]
        script = (
            "const src = process.argv[1];\n"
            "const RESOLVES_TO = eval('(' + src + ')');\n"
            "const cases = JSON.parse(process.argv[2]);\n"
            "const out = cases.map(function (c) {\n"
            "  return { name: c.name, got: !!RESOLVES_TO(c.facts), want: c.want };\n"
            "});\n"
            "process.stdout.write(JSON.stringify(out));\n"
        )
        result = subprocess.run(
            [_node(), "-e", script, daemon._RESOLVES_TO_JS, json.dumps(scenarios)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for case in json.loads(result.stdout):
            with self.subTest(scenario=case["name"]):
                self.assertEqual(case["got"], case["want"])


if __name__ == "__main__":
    unittest.main()
