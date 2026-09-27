from __future__ import annotations

from typing import AsyncIterator, Dict, Optional

import httpx

from ..config import Settings
from ..providers.base import truncate


class WorkspaceClient:
    """HTTP client for the agent that runs on the remote computer.

    Same three apps as before -- browser, files, terminal -- reached over a
    base URL instead of a QEMU-forwarded port.  Nothing about the agent's API
    changed; only the addressing did.
    """

    def __init__(self, base_url: str, settings: Settings):
        self.base_url = base_url.rstrip("/")
        self.settings = settings

    async def status(self) -> Optional[Dict[str, object]]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/status")
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            return None
        return None

    async def exec_tool(self, tool: str, args: Dict[str, object], timeout: int = 120) -> Dict[str, object]:
        payload = {"tool": tool, "args": args, "timeout": timeout}
        async with httpx.AsyncClient(timeout=max(timeout, 30) + 10.0) as client:
            resp = await client.post(f"{self.base_url}/tool/exec", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def screenshot(self) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.base_url}/screen")
                if resp.status_code == 200:
                    header = resp.headers.get("x-screen", "")
                    if header:
                        return header
                    import base64
                    return base64.b64encode(resp.content).decode()
        except Exception:
            return None
        return None

    async def run(self, name: str, args: Dict[str, object]) -> AsyncIterator[Dict[str, object]]:
        tool = {
            "read_file": "read_file",
            "write_file": "write_file",
            "list_dir": "list_dir",
            "shell": "shell",
        }.get(name, name)
        try:
            result = await self.exec_tool(tool, args)
        except Exception as exc:
            yield {"type": "result", "output": "", "error": f"computer call failed: {exc}"}
            return
        output = str(result.get("output", ""))
        error = str(result.get("error", ""))
        title = str(result.get("title", ""))
        url = str(result.get("url", ""))
        image = str(result.get("image", ""))
        fmt = str(result.get("format", "")) or ("jpeg" if image.startswith("/9j/") else "png")
        yield {
            "type": "result",
            "output": truncate(output, self.settings.max_text_chars),
            "error": error,
            "title": title,
            "url": url,
            "image": image,
            "format": fmt,
        }
