from __future__ import annotations

from typing import AsyncIterator, Dict, Optional

from ..config import Settings
from ..email.tools import NOTIFICATION_TOOL_NAMES, run_notification_tool
from ..routines.tools import ROUTINE_TOOL_NAMES, run_routine_tool
from .local import LocalExecutor
from .workspace import WorkspaceClient


class Executor:
    """Runs a tool against the remote computer, locally, or the restricted runtime.

    The tool names are unchanged, so the agents and the UI do not care where
    the work happens.  What changed is who is allowed to do it: the ordinary
    agent's network tools are routed through the restricted runtime, and the
    unrestricted ``browser`` tool is gone, so a personal Chrome is never a path
    an ordinary agent can take.

    Routing, in order:

    * ``browse`` always goes to the restricted runtime; with restricted
      browsing off it refuses rather than falling back to any browser.
    * ``fetch_url`` / ``web_search`` / ``shell`` go to the restricted runtime
      when restricted browsing is enabled, and to the computer/local path only
      when it is off -- so an operator who has not enabled the sandbox keeps
      today's behaviour, and an operator who has enabled it cannot be bypassed.
    * everything else (files, and the computer's own tools) is unchanged.
    """

    #: Tools that run on the remote computer when it is attached.  ``browse`` is
    #: deliberately absent: it is the restricted runtime's, not the personal
    #: browser's, and ``browser`` is deliberately absent for the same reason.
    COMPUTER_TOOLS = {"shell", "read_file", "write_file", "list_dir", "browser_screenshot", "web_search", "fetch_url"}

    #: Tools the restricted runtime owns whenever it is enabled.
    RESTRICTED_TOOLS = {"browse", "fetch_url", "web_search", "shell"}

    def __init__(
        self,
        settings: Settings,
        computer: Optional[WorkspaceClient] = None,
        restricted=None,
        db=None,
    ):
        self.settings = settings
        self.local = LocalExecutor(settings)
        self.computer = computer
        self.computer_active = computer is not None
        self.restricted = restricted
        # The owner-notification tools need the database (drafts, sends, audit).
        # It is optional so a bare executor used outside the app still works; a
        # notification call without it fails closed.
        self.db = db

    def use_computer(self, computer: Optional[WorkspaceClient]) -> None:
        self.computer = computer
        self.computer_active = computer is not None

    def use_restricted(self, restricted) -> None:
        self.restricted = restricted

    @property
    def _restricted_on(self) -> bool:
        return self.restricted is not None and getattr(self.restricted, "enabled", False)

    @property
    def vm(self) -> Optional[WorkspaceClient]:  # backwards-compatible alias
        return self.computer

    @vm.setter
    def vm(self, value: Optional[WorkspaceClient]) -> None:
        self.computer = value
        self.computer_active = value is not None

    @property
    def vm_active(self) -> bool:
        return self.computer_active

    @vm_active.setter
    def vm_active(self, value: bool) -> None:
        # A caller clearing the flag means "stop using the computer"; never
        # leave the client attached while pretending it is off.
        self.computer_active = bool(value and self.computer is not None)

    async def run(self, name: str, args: Dict[str, object]) -> AsyncIterator[Dict[str, object]]:
        # Owner notifications are served entirely by this process, but unlike the
        # routine tools they need the database.  They are routed here first so a
        # notification name can never fall through to a computer, the restricted
        # runtime or the local shell.
        if name in NOTIFICATION_TOOL_NAMES:
            if self.db is None:
                yield {
                    "type": "result",
                    "output": "",
                    "error": "owner notifications are unavailable: no database is attached",
                }
            else:
                yield await run_notification_tool(name, args, self.settings, self.db)
            return

        # Routine management and the clock are served entirely by this process
        # (which in turn calls the edge); they never touch the computer, the
        # restricted runtime or the local shell.
        if name in ROUTINE_TOOL_NAMES:
            yield await run_routine_tool(name, args, self.settings)
            return

        # ``browse`` is the restricted browser or nothing.  It never falls
        # through to a personal browser, with or without the runtime enabled.
        if name == "browse":
            if self._restricted_on:
                yield await self.restricted.browse(args)
            else:
                yield {
                    "type": "result",
                    "output": "",
                    "error": "restricted browsing is disabled; the unrestricted browser is not available to agents",
                }
            return

        if name in self.RESTRICTED_TOOLS and self._restricted_on:
            if name == "fetch_url":
                yield await self.restricted.fetch(str(args.get("url", "")))
            elif name == "web_search":
                yield await self.restricted.search(str(args.get("query", "")))
            elif name == "shell":
                yield await self.restricted.run_shell(str(args.get("command", "")), int(args.get("timeout", 30)))
            return

        if name in self.COMPUTER_TOOLS and self.computer_active and self.computer is not None:
            async for ev in self.computer.run(name, args):
                yield ev
        else:
            async for ev in self.local.run(name, args):
                yield ev
