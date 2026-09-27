from __future__ import annotations

import asyncio
import html
import json
import os
import re
from typing import AsyncIterator, Dict

import httpx

from ..config import Settings
from .base import ExecutionEvent
from ..providers.base import truncate

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RAG-Agents/1.0"


class LocalExecutor:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def run(self, name: str, args: Dict[str, object]) -> AsyncIterator[ExecutionEvent]:
        if name == "web_search":
            async for ev in self.web_search(str(args.get("query", ""))):
                yield ev
        elif name == "fetch_url":
            async for ev in self.fetch_url(str(args.get("url", ""))):
                yield ev
        elif name == "shell":
            async for ev in self.shell(str(args.get("command", "")), int(args.get("timeout", 30))):
                yield ev
        elif name == "read_file":
            yield await self.read_file(str(args.get("path", "")))
        elif name == "write_file":
            yield await self.write_file(str(args.get("path", "")), str(args.get("content", "")))
        elif name == "list_dir":
            yield await self.list_dir(str(args.get("path", ".")))
        else:
            yield {"type": "result", "output": "", "error": f"tool '{name}' not available locally"}

    async def shell(self, command: str, timeout: int) -> AsyncIterator[ExecutionEvent]:
        if not command.strip():
            yield {"type": "result", "output": "", "error": "empty command"}
            return
        yield {"type": "output", "content": f"$ {command}\n"}
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        chunks: list = []
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout)
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\n")
                chunks.append(text)
                yield {"type": "output", "content": text + "\n"}
        except asyncio.TimeoutError:
            proc.kill()
            yield {"type": "output", "content": f"\n[timeout after {timeout}s]\n"}
        try:
            stderr = await asyncio.wait_for(proc.stderr.read(), 5)
        except Exception:
            stderr = b""
        await proc.wait()
        err = stderr.decode("utf-8", "replace").strip()
        code = proc.returncode or 0
        output = "\n".join(chunks)
        if err:
            output = (output + "\n[stderr]\n" + err).strip()
        yield {
            "type": "result",
            "output": truncate(output, self.settings.max_text_chars),
            "error": "" if code == 0 else f"exit code {code}",
        }

    async def web_search(self, query: str) -> AsyncIterator[ExecutionEvent]:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
        results = []
        error = ""
        try:
            async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": _USER_AGENT}) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
            abstract = data.get("AbstractText", "").strip()
            if data.get("AbstractURL"):
                results.append({"title": data.get("Heading", "Abstract"), "url": data["AbstractURL"], "snippet": abstract})
            for topic in flatten_topics(data.get("RelatedTopics") or []):
                if "Text" in topic and "FirstURL" in topic:
                    results.append({"title": topic.get("Text", "")[:80], "url": topic["FirstURL"], "snippet": topic.get("Text", "")})
        except Exception as exc:
            error = f"search failed: {exc}"
        if results:
            text = "Search results:\n" + "\n".join(
                f"- {r['title']}: {r['url']}\n  {r['snippet'][:200]}" for r in results[:6]
            )
        elif not error:
            error = "no results returned (DuckDuckGo instant answer)"
        yield {"type": "result", "output": truncate(text if not error else "", self.settings.max_web_chars), "error": error}

    async def fetch_url(self, page_url: str) -> AsyncIterator[ExecutionEvent]:
        error = ""
        text = ""
        try:
            async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": _USER_AGENT}, follow_redirects=True) as client:
                resp = await client.get(page_url)
                resp.raise_for_status()
                text = resp.text
        except Exception as exc:
            error = f"fetch failed: {exc}"
        if not error:
            raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
            raw = re.sub(r"<[^>]+>", "\n", raw)
            raw = html.unescape(raw)
            raw = re.sub(r"\n{3,}", "\n\n", raw)
            text = raw.strip()
        if not error and not text:
            error = "page produced no readable text"
        yield {
            "type": "result",
            "output": truncate(text, self.settings.max_web_chars),
            "error": error,
        }

    async def read_file(self, path: str) -> ExecutionEvent:
        try:
            if not os.path.exists(path):
                return {"type": "result", "output": "", "error": f"file not found: {path}"}
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            return {"type": "result", "output": truncate(content, self.settings.max_text_chars), "error": ""}
        except Exception as exc:
            return {"type": "result", "output": "", "error": str(exc)}

    async def write_file(self, path: str, content: str) -> ExecutionEvent:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return {"type": "result", "output": f"wrote {len(content)} chars to {path}", "error": ""}
        except Exception as exc:
            return {"type": "result", "output": "", "error": str(exc)}

    async def list_dir(self, path: str) -> ExecutionEvent:
        try:
            entries = []
            for entry in sorted(os.listdir(path)):
                full = os.path.join(path, entry)
                import time as _time
                try:
                    mtime = int(os.path.getmtime(full))
                except Exception:
                    mtime = 0
                ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(mtime))
                if os.path.isdir(full):
                    entries.append(f"dir\t{ts}\t{entry}")
                else:
                    entries.append(f"file\t{os.path.getsize(full)}\t{ts}\t{entry}")
            if not os.path.exists(path):
                return {"type": "result", "output": "", "error": f"path not found: {path}"}
            return {"type": "result", "output": "\n".join(entries) or "(empty)", "error": ""}
        except Exception as exc:
            return {"type": "result", "output": "", "error": str(exc)}


def flatten_topics(topics) -> list:
    out = []
    for t in topics:
        if isinstance(t, list):
            out.extend(flatten_topics(t))
        elif isinstance(t, dict):
            out.append(t)
    return out