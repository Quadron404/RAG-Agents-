"""The remote computer: a GitHub Codespace, driven over HTTP.

This replaces the old QEMU manager.  There is no hypervisor and no disk image
any more -- the "computer" *is* the Codespace container, and it runs a real
Ubuntu userland with real Google Chrome on a real X display.  What this module
does is the same job the old one did, minus the emulation:

  * answer "is the computer there?"          -> probe the agent's /status
  * "power it on"                             -> ask the agent to ensure the stack
  * "power it off"                            -> ask the agent to stop the browser
  * "where is its screen?"                    -> the loopback RFB port

The agent is reachable on loopback because the whole product runs in the same
Codespace.  If you ever split them, set WORKSPACE_BASE_URL to a private
Codespace URL and nothing else has to change.

Multi-user note: the agent is addressed by *base URL*, not by user id, and the
ports are settings rather than a per-user counter.  That is deliberate.  When
per-user computers arrive, each one becomes its own (base URL, port pair)
instance of this class rather than a special case threaded through the callers.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

import httpx

from ..config import Settings


class WorkspaceManager:
    """Supervises the remote computer from the RAG Agents backend."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.workspace_base_url
        self._sysinfo: Dict[str, object] = {}
        self._agent_up = False

    # --- addressing --------------------------------------------------------

    @property
    def screen_target(self) -> Optional[Tuple[str, int]]:
        """Loopback host:port of the live framebuffer's RFB server.

        None when the screen is disabled.  The port is x11vnc's, which the
        agent pins to 127.0.0.1 -- this backend and the browser share a
        machine, but the VNC protocol still never leaves it.
        """
        if not self.settings.screen_vnc_enabled:
            return None
        return ("127.0.0.1", int(self.settings.screen_vnc_port))

    @property
    def websockify_port(self) -> int:
        return int(self.settings.screen_ws_port)

    # --- health ------------------------------------------------------------

    async def _call(self, method: str, path: str, timeout: float = 8.0, **kw):
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{self.base_url}{path}", **kw)
            resp.raise_for_status()
            return resp

    async def is_up(self) -> bool:
        """True when the agent answers.  Cheap enough to poll."""
        try:
            resp = await self._call("GET", "/status", timeout=4.0)
            self._agent_up = resp.status_code == 200
        except Exception:
            self._agent_up = False
        return self._agent_up

    @property
    def agent_up(self) -> bool:
        return self._agent_up

    async def sysinfo(self) -> Dict[str, object]:
        try:
            resp = await self._call("GET", "/sysinfo", timeout=6.0)
            data = resp.json()
            if isinstance(data, dict):
                self._sysinfo = data
        except Exception:
            pass
        return self._sysinfo

    async def display_status(self) -> Dict[str, object]:
        try:
            resp = await self._call("GET", "/display/status", timeout=5.0)
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # --- power -------------------------------------------------------------

    async def start(self) -> Dict[str, object]:
        """Bring the computer up.

        Idempotent by design: the agent supervises X, the window manager, the
        VNC server and the browser, so asking twice costs nothing and asking
        for an already-running computer is not an error.  The UI's power button
        is a "make sure it is on" button, not a hypervisor start.
        """
        try:
            resp = await self._call("POST", "/display/ensure", timeout=60.0)
            display = resp.json()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"the computer is not reachable at {self.base_url}: {exc}",
            }
        return await self._result("computer is online", display)

    async def stop(self) -> Dict[str, object]:
        """Close the browser session but leave X and the VNC server up.

        Killing X would take the screen down with it and there would be nothing
        to look at until the next start, which is a worse outcome than a dark
        Chrome window the user can restart.
        """
        try:
            resp = await self._call("POST", "/display/stop", timeout=30.0)
            display = resp.json()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"the computer is not reachable at {self.base_url}: {exc}",
            }
        return await self._result("computer is off", display, ok=False)

    async def restart_browser(self) -> Dict[str, object]:
        try:
            resp = await self._call("POST", "/display/chromium/restart", timeout=60.0)
            display = resp.json()
        except Exception as exc:
            return {"ok": False, "error": f"could not restart the browser: {exc}"}
        return await self._result("browser restarted", display)

    async def _result(self, msg: str, display: Dict[str, object], ok: bool = True) -> Dict[str, object]:
        """Shape the agent's display status into what the UI already expects."""
        info = await self.sysinfo()
        mem = info.get("mem") or {}
        cpu = info.get("cpu") or {}
        chrome_up = bool(display.get("chromium"))
        return {
            "ok": ok,
            "running": ok and chrome_up,
            "msg": msg if chrome_up else f"{msg} (no browser yet)",
            # The Codespace is the whole machine now, so the UI reports what it
            # actually has rather than a hypervisor allocation that no longer
            # exists.
            "mem_mb": int(mem.get("total_kb", 0) or 0) // 1024,
            "cpus": int(cpu.get("cores", 0) or 0),
            "workspace": info.get("workspace", ""),
            "display": display,
        }

    # --- inventory ---------------------------------------------------------

    def list_computers(self) -> List[Dict[str, object]]:
        """One entry: the single computer this deployment talks to.

        Kept as a list because the UI iterates it, and because a per-user
        deployment returns one entry per user here without touching callers.
        """
        return [
            {
                "user_id": "default",
                "base_url": self.base_url,
                "screen_port": self.settings.screen_vnc_port,
                "websockify_port": self.websockify_port,
                "reachable": self._agent_up,
            }
        ]

    def describe(self) -> Dict[str, object]:
        target = self.screen_target
        return {
            "agent": self.base_url,
            "agent_up": self._agent_up,
            "screen_host": target[0] if target else "",
            "screen_port": target[1] if target else 0,
            "websockify_port": self.websockify_port,
            "public_ws_url": self.settings.computer_ws_url,
        }
