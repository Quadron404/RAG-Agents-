from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import AsyncIterator, Dict, Optional

from ..config import Settings
from .local import LocalExecutor
from .workspace import WorkspaceClient


class Executor:
    """Runs a tool against the remote computer, or locally if it is unreachable.

    The tool names are unchanged, so the agents and the UI do not care where
    the work happens.  What changed is only that "the VM" is now a Codespace
    reached over HTTP.
    """

    COMPUTER_TOOLS = {"shell", "read_file", "write_file", "list_dir", "browser", "browser_screenshot", "web_search", "fetch_url"}

    def __init__(self, settings: Settings, computer: Optional[WorkspaceClient] = None):
        self.settings = settings
        self.local = LocalExecutor(settings)
        self.computer = computer
        self.computer_active = computer is not None

    def use_computer(self, computer: Optional[WorkspaceClient]) -> None:
        self.computer = computer
        self.computer_active = computer is not None

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
        if name in self.COMPUTER_TOOLS and self.computer_active and self.computer is not None:
            async for ev in self.computer.run(name, args):
                yield ev
        else:
            async for ev in self.local.run(name, args):
                yield ev
