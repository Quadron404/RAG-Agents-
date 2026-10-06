"""The remote actions computer control is allowed to take.

This is the boundary.  Everything above it reasons about JSON; everything below
it talks to the agent running on the remote computer.  The allowlist is expressed
by the fact that this class exposes exactly five methods, each mapping to one
route, and there is no method that takes a shell command, a file path, or a URL
the model could assemble into a local one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

from ..workspace.manager import WorkspaceManager

from .ui_state import state_signature

log = logging.getLogger(__name__)


def _trace_click(label: str, x: float, y: float, result: Dict[str, Any]) -> None:
    """One line per pointer action, with every stage of the coordinate contract.

    model -> validated -> executed -> actual.  When a click misses, this is the
    line that says which of those four disagreed, instead of leaving it to be
    guessed at from a screenshot.
    """
    actual_x = result.get("actual_x")
    actual_y = result.get("actual_y")
    log.info(
        "%s screen=%sx%s model=(%s,%s) executed=(%s,%s) actual=(%s,%s) %s",
        label,
        result.get("display_width"), result.get("display_height"),
        x, y,
        result.get("x"), result.get("y"),
        actual_x, actual_y,
        "LANDED" if result.get("landed") else ("DRIFTED" if actual_x is not None else "UNVERIFIED"),
    )


class ComputerError(RuntimeError):
    """A remote action could not be carried out.

    Carries a message safe to show a model or a user, because it is derived
    from the agent's own error field and from nothing the caller supplied.
    """


class RemoteComputer:
    """The actions this feature is allowed to take on the remote machine.

    The manager is injected rather than imported from a module-level singleton,
    which is what lets the whole loop be tested without a Codespace.

    There is no method here that takes a shell command, a key argument or a file
    path, and no method that forwards a model's string to the agent unexamined:
    `key` takes an already-validated combo, and `type` takes text that the agent
    passes to xdotool as a single argument with no shell involved.
    """

    def __init__(
        self,
        manager: WorkspaceManager,
        settle_ms: int = 1400,
        settle_ms_click: int = 900,
        settle_ms_typing: int = 700,
        state_settle_ms: int = 600,
    ) -> None:
        self.manager = manager
        self.settle_ms = settle_ms
        self.settle_ms_click = settle_ms_click
        # Typing does not move the page under the caret, so it needs the least
        # wait of the three. It is still not zero: the keystrokes are sent one
        # at a time with a delay between them, and the last few land after the
        # call has already returned.
        self.settle_ms_typing = settle_ms_typing
        # How long a state-changing action may take to stop changing the page,
        # measured by polling the page itself rather than by a fixed sleep.
        self.state_settle_ms = state_settle_ms

    async def _post(
        self, path: str, payload: Dict[str, Any], timeout: float = 30.0
    ) -> Dict[str, Any]:
        response = await self.manager.post_json(path, payload, timeout=timeout)
        if not isinstance(response, dict):
            raise ComputerError(f"the remote computer returned no result for {path}")
        return response

    async def _settle(self, ms: int) -> None:
        """Let the page finish reacting before the next screenshot.

        Without this the model is shown a half-painted page and clicks a target
        that is not there yet -- the single most common source of a control loop
        that appears to be working and is not.
        """
        if ms > 0:
            await asyncio.sleep(ms / 1000.0)

    async def _settle_state(self) -> None:
        """Wait for the page to stop changing, or give up trying.

        A fixed sleep settles an action that hangs the page, but it cannot say
        whether the page has actually stopped -- so the state read that follows
        can capture a page that is still half-way through reacting, and a model
        that trusts it acts on a state that no longer exists.

        This polls the page itself: two consecutive reads that agree mean it is
        still, and the ``state_settle_ms`` budget bounds the wait when it is
        not.  A browser that will not answer cannot be waited on, so a read that
        fails ends the wait immediately rather than burning the budget hunting.
        """
        budget = self.state_settle_ms
        if budget <= 0:
            return
        deadline = time.monotonic() + budget / 1000.0
        interval = 0.1
        previous = ""
        while True:
            try:
                current = await self.state()
                signature = state_signature(current)
            except Exception:
                return
            if previous and signature and signature == previous:
                return
            previous = signature
            if time.monotonic() + interval >= deadline:
                return
            await asyncio.sleep(interval)

    async def navigate(self, url: str) -> Dict[str, Any]:
        result = await self._post("/computer/navigate", {"url": url})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "navigation failed"))
        await self._settle(self.settle_ms)
        await self._settle_state()
        return result

    async def search(self, query: str) -> Dict[str, Any]:
        result = await self._post("/computer/search", {"query": query})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "search failed"))
        await self._settle(self.settle_ms)
        await self._settle_state()
        return result

    async def click(self, x: float, y: float) -> Dict[str, Any]:
        """Click a point in the screenshot's own pixel grid.

        The coordinates go to the agent unchanged and become
        `xdotool mousemove --sync <x> <y> click 1` on the real display, so the
        grid the model was shown and the grid X acts on are the same one.  The
        reply carries the pointer position read back from X afterwards, logged
        here so a click that drifted off its target is visible rather than
        silent.
        """
        result = await self._post("/computer/click", {"x": int(x), "y": int(y)})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "click failed"))
        _trace_click("CLICK", x, y, result)
        # The agent already delivered the synchronous X event, so there is no
        # fixed application-level sleep after a click -- but the *page* it
        # landed on can still be mid-reaction, and the next state read must be
        # of the page after the click settled, not of one that is still
        # changing.  That is what polling the page measures; a fixed sleep
        # cannot.
        await self._settle_state()
        return result

    async def type_text(self, text: str) -> Dict[str, Any]:
        """Type into whatever has focus on the remote display."""
        result = await self._post("/computer/type", {"text": text}, timeout=60.0)
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "typing failed"))
        await self._settle(self.settle_ms_typing)
        await self._settle_state()
        return result

    async def key(self, combo: str) -> Dict[str, Any]:
        """Press one key or combo.

        `combo` has already been through normalize_key, so it is a list of names
        from the allowlist joined by "+".  The agent checks it against its own
        copy of that allowlist regardless of what arrives here.
        """
        result = await self._post("/computer/key", {"key": combo})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "key press failed"))
        # The full page settle, not the click one: ENTER in an address bar or a
        # search box is usually a navigation, and a screenshot taken before the
        # page has moved is a screenshot of the old page.
        await self._settle(self.settle_ms)
        await self._settle_state()
        return result

    async def scroll(self, delta_y: int) -> Dict[str, Any]:
        """Scroll the focused remote window. Positive is down, negative is up."""
        result = await self._post("/computer/scroll", {"delta_y": int(delta_y)})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "scroll failed"))
        await self._settle(self.settle_ms_click)
        await self._settle_state()
        return result

    async def move(self, x: float, y: float) -> Dict[str, Any]:
        """Move the remote pointer without clicking."""
        result = await self._post("/computer/move", {"x": int(x), "y": int(y)})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "pointer move failed"))
        _trace_click("MOVE", x, y, result)
        # Pointer movement is already an X event; no artificial delay.
        return result

    async def screenshot(self) -> Tuple[Optional[str], int, int]:
        """A JPEG of the real display, as (base64, width, height).

        This is the whole screen, not a page viewport, which is what makes the
        coordinates in it -- the tab strip, the address bar, the page -- all
        one coordinate system, and all clickable by the same call.

        The size returned is the size of the image that was actually captured,
        read out of the JPEG by the agent.  It is deliberately not a value the
        app decided: the model is told this number as its coordinate system, so
        it has to describe the bytes in front of the model and nothing else.
        There is no resize anywhere between the two -- the same base64 string is
        handed to the provider -- so a coordinate the model returns is in this
        grid already, with no conversion left to get wrong.
        """
        # 45s: the capture is an ffmpeg invocation on a busy remote machine, and
        # a screenshot that times out is a false negative the model would then be
        # asked to reason about as a blank screen.
        result = await self._post("/computer/screen", {"draw_mouse": True}, timeout=45.0)
        if not result.get("ok") or not result.get("image"):
            raise ComputerError(str(result.get("error") or "could not capture the screen"))
        width = int(result.get("width") or 0)
        height = int(result.get("height") or 0)
        if not (width > 0 and height > 0):
            raise ComputerError(
                f"the agent reported a {width}x{height} screenshot; refusing to "
                "reason about coordinates in an unknown grid"
            )
        # The agent compares the image against the X display and says so here.
        # A mismatch is not fatal -- the run continues on the image's own grid --
        # but it is logged, because it means every coordinate is off by a ratio.
        if result.get("geometry_matches") is False:
            log.warning(
                "computer: screenshot is %dx%d but the X display is %sx%s; "
                "coordinates will be off by a ratio",
                width, height,
                result.get("display_width"), result.get("display_height"),
            )
        return result["image"], width, height

    async def state(self) -> Dict[str, Any]:
        """The current URL and title, read out of the real browser."""
        result = await self._post("/computer/state", {})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "could not read the browser state"))
        return result
