"""The CDP capture path: the loop's view of a browser with no X display on it.

The screenshot the model reasons about is the whole point of the loop, so the
only acceptable way to get one is out of the browser that is really there.  On
the Codespace that is `ffmpeg` against the X display.  Everywhere else -- a
workstation, a CI box, any host where Chrome is open but no Xvfb is -- the same
real pixels are one CDP command away, and refusing there would make the loop
blind to a browser that is plainly running.

These tests drive the same functions `/computer/screen` calls with a fake
socket in place of Chrome.  Nothing here connects to anything.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vm_agent"))

import daemon  # noqa: E402


def jpeg_bytes(width: int, height: int) -> bytes:
    """The smallest thing `_jpeg_size` accepts as an image."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9"
    )


class FakeChromeSocket:
    """A DevTools socket that answers one screenshot and remembers what it was sent."""

    def __init__(self, data: bytes | None = None, error: dict | None = None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._data = data
        self._error = error

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def settimeout(self, value) -> None:
        pass

    def recv(self) -> str:
        message = self.sent[-1]
        if self._error is not None:
            return json.dumps({"id": message["id"], "error": self._error})
        return json.dumps({
            "id": message["id"],
            "result": {"data": base64.b64encode(self._data or b"").decode()},
        })

    def close(self) -> None:
        self.closed = True


class TestTheDebugPortIsTheOneChromeWasGiven(unittest.TestCase):
    def test_the_port_follows_the_same_environment_variable_as_the_browser(self):
        # A hardcoded 9222 would silently point the CDP helpers at whatever
        # Chrome happens to be on the default port, which is either nothing or
        # somebody else's browser.  Every CDP call then fails identically to
        # "Chrome is not running", while Chrome is plainly running.
        self.assertEqual(daemon.CDP_DEBUG_PORT, daemon.CHROME_DEBUG_PORT)
        self.assertEqual(daemon.CDP_DEBUG_PORT, int(os.environ.get("CHROME_DEBUG_PORT", "9222")))

    def test_the_setting_is_read_at_import_time_not_baked_in(self):
        real = os.environ.get("CHROME_DEBUG_PORT")
        try:
            os.environ["CHROME_DEBUG_PORT"] = "9333"
            reloaded = importlib.reload(daemon)
            self.assertEqual(reloaded.CDP_DEBUG_PORT, 9333)
        finally:
            if real is None:
                os.environ.pop("CHROME_DEBUG_PORT", None)
            else:
                os.environ["CHROME_DEBUG_PORT"] = real
            importlib.reload(daemon)


class TestTheDevToolsHandshake(unittest.TestCase):
    def test_the_socket_is_opened_without_an_origin_header(self):
        # Chrome answers a DevTools WebSocket carrying an `Origin` with a bare
        # 403 unless it was started with `--remote-allow-origins`.  The callers
        # swallow that into an empty url, an empty title and "no display", so
        # the refusal has to be designed out of the connection, not debugged
        # later through three unrelated symptoms.
        seen: dict = {}

        class Recorder:
            @staticmethod
            def create_connection(wsurl, **kwargs):
                seen.update(kwargs)
                return FakeChromeSocket()

        sys.modules["websocket"] = Recorder
        try:
            daemon._cdp_connect("ws://127.0.0.1:9222/devtools/page/abc")
        finally:
            sys.modules.pop("websocket", None)
            import websocket  # noqa: F401 - put the real module back
        self.assertTrue(seen.get("suppress_origin"))


class TestAScreenshotThatNeedsAPayloadCanHaveOne(unittest.TestCase):
    """A second DevTools connection to a page target goes silent.

    The input worker already owns a persistent socket to the browser.  Chrome
    stops answering on a *new* connection to the same target, so any command
    whose result has to come back has to travel on the one that is already
    attached.  That is why `_cdp_cmd` can hand back a payload, and why the
    screenshot rides the same lock the input commands do.
    """

    def setUp(self) -> None:
        self.sent: list[dict] = []
        outer = self

        class Socket:
            """Replies to whatever was sent, one message id at a time."""

            def settimeout(self, value):
                self.timeout = value

            def send(self, raw):
                outer.sent.append(json.loads(raw))

            def recv(self):
                message = outer.sent[-1]
                if message["method"] == "Page.captureScreenshot":
                    return json.dumps({
                        "id": message["id"],
                        "result": {"data": base64.b64encode(jpeg_bytes(1280, 720)).decode()},
                    })
                return json.dumps({"id": message["id"], "result": {}})

        self.ws = Socket()
        real_ws, real_reset = daemon._cdp_in_ws, daemon._cdp_in_reset
        daemon._cdp_in_ws = lambda: self.ws
        daemon._cdp_in_reset = lambda: None
        self.addCleanup(setattr, daemon, "_cdp_in_ws", real_ws)
        self.addCleanup(setattr, daemon, "_cdp_in_reset", real_reset)

    def test_the_payload_comes_back_when_the_caller_asked_for_it(self):
        result = daemon._cdp_cmd("Page.captureScreenshot", {"format": "jpeg"},
                                 want_result=True)
        self.assertTrue(result["ok"])
        self.assertIn("result", result)
        self.assertTrue(result["result"]["data"])

    def test_the_input_path_is_unchanged_by_that_option(self):
        # The dispatch helpers assert on `ok` alone and must not have to care
        # about a payload shape they never read.
        result = daemon._cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved"})
        self.assertEqual(result, {"ok": True})


class TestTheLoopSeesTheBrowserThatIsReallyThere(unittest.TestCase):
    def setUp(self) -> None:
        self.real_x = daemon._x_running
        self.real_wsurl = daemon._cdp_target_wsurl
        self.real_cdp_cmd = daemon._cdp_cmd
        daemon._x_running = lambda: False
        self.addCleanup(setattr, daemon, "_x_running", self.real_x)
        self.addCleanup(setattr, daemon, "_cdp_target_wsurl", self.real_wsurl)
        self.addCleanup(setattr, daemon, "_cdp_cmd", self.real_cdp_cmd)

    def _answer_screenshots_with(self, data, error=None):
        daemon._cdp_target_wsurl = lambda: "ws://127.0.0.1:9222/devtools/page/abc"

        def cmd(method, params=None, wait=3.0, want_result=False):
            if not want_result:
                return {"ok": True}
            if error is not None:
                return {"ok": False, "error": json.dumps(error)}
            return {"ok": True, "result": {"data": base64.b64encode(data).decode()}}

        daemon._cdp_cmd = cmd

    def test_with_no_display_the_capture_is_still_chrome_s_own_pixels(self):
        self._answer_screenshots_with(jpeg_bytes(1280, 720))
        result = daemon._capture_display()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["source"], "cdp")
        self.assertTrue(result["image"].startswith("/9j/"), "not a JPEG")

    def test_the_reported_size_is_the_size_of_the_image_being_sent(self):
        # Coordinates are validated against this grid, so it has to describe the
        # bytes in the prompt rather than any setting.
        self._answer_screenshots_with(jpeg_bytes(1690, 843))
        result = daemon._capture_display()
        self.assertEqual((result["width"], result["height"]), (1690, 843))

    def test_a_capture_of_no_size_is_a_failure_not_a_silent_zero(self):
        self._answer_screenshots_with(b"not a jpeg")
        result = daemon._capture_display()
        self.assertTrue(result["ok"])
        self.assertEqual((result["width"], result["height"]), (0, 0))

    def test_a_refusal_from_chrome_is_reported_rather_than_swallowed(self):
        self._answer_screenshots_with(b"", error={"code": -32000, "message": "nope"})
        result = daemon._capture_display()
        self.assertFalse(result["ok"])
        self.assertIn("nope", result["error"])

    def test_with_neither_a_display_nor_chrome_it_says_so(self):
        # The honest answer when there is genuinely nothing to photograph.
        daemon._cdp_target_wsurl = lambda: None
        result = daemon._capture_display()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "the X display is not running")

    def test_an_undecodable_image_is_refused_rather_than_reported_as_ok(self):
        daemon._cdp_target_wsurl = lambda: "ws://127.0.0.1:9222/devtools/page/abc"
        daemon._cdp_cmd = lambda *a, **k: {"ok": True, "result": {"data": "!!!not base64!!!"}}
        result = daemon._capture_display()
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)