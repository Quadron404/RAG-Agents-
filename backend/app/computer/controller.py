"""The remote actions computer control is allowed to take.

This is the boundary.  Everything above it reasons about JSON; everything below
it talks to the agent running on the remote computer.  The allowlist is expressed
by the fact that this class exposes exactly five methods, each mapping to one
route, and there is no method that takes a shell command, a file path, or a URL
the model could assemble into a local one.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

from ..workspace.manager import WorkspaceManager


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
    ) -> None:
        self.manager = manager
        self.settle_ms = settle_ms
        self.settle_ms_click = settle_ms_click
        # Typing does not move the page under the caret, so it needs the least
        # wait of the three. It is still not zero: the keystrokes are sent one
        # at a time with a delay between them, and the last few land after the
        # call has already returned.
        self.settle_ms_typing = settle_ms_typing

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

    async def navigate(self, url: str) -> Dict[str, Any]:
        result = await self._post("/computer/navigate", {"url": url})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "navigation failed"))
        await self._settle(self.settle_ms)
        return result

    async def search(self, query: str) -> Dict[str, Any]:
        result = await self._post("/computer/search", {"query": query})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "search failed"))
        await self._settle(self.settle_ms)
        return result

    async def click(self, x: float, y: float) -> Dict[str, Any]:
        result = await self._post("/computer/click", {"x": int(x), "y": int(y)})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "click failed"))
        await self._settle(self.settle_ms_click)
        return result

    async def type_text(self, text: str) -> Dict[str, Any]:
        """Type into whatever has focus on the remote display."""
        result = await self._post("/computer/type", {"text": text}, timeout=60.0)
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "typing failed"))
        await self._settle(self.settle_ms_typing)
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
        return result

    async def scroll(self, delta_y: int) -> Dict[str, Any]:
        """Scroll the focused remote window. Positive is down, negative is up."""
        result = await self._post("/computer/scroll", {"delta_y": int(delta_y)})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "scroll failed"))
        await self._settle(self.settle_ms_click)
        return result

    async def move(self, x: float, y: float) -> Dict[str, Any]:
        """Move the remote pointer without clicking."""
        result = await self._post("/computer/move", {"x": int(x), "y": int(y)})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "pointer move failed"))
        await self._settle(self.settle_ms_click)
        return result

    async def screenshot(self) -> Tuple[Optional[str], int, int]:
        """A JPEG of the real display, as (base64, width, height).

        This is the whole screen, not a page viewport, which is what makes the
        coordinates in it -- the tab strip, the address bar, the page -- all
        one coordinate system, and all clickable by the same call.
        """
        # 45s: the capture is an ffmpeg invocation on a busy remote machine, and
        # a screenshot that times out is a false negative the model would then be
        # asked to reason about as a blank screen.
        result = await self._post("/computer/screen", {"draw_mouse": True}, timeout=45.0)
        if not result.get("ok") or not result.get("image"):
            raise ComputerError(str(result.get("error") or "could not capture the screen"))
        return result["image"], int(result.get("width") or 0), int(result.get("height") or 0)

    async def state(self) -> Dict[str, Any]:
        """The current URL and title, read out of the real browser."""
        result = await self._post("/computer/state", {})
        if not result.get("ok"):
            raise ComputerError(str(result.get("error") or "could not read the browser state"))
        return result
