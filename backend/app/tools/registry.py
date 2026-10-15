from __future__ import annotations

import json
from typing import List

from ..email.tools import notification_tools
from ..routines.tools import routine_tools
from .base import Tool


def worker_tools() -> List[Tool]:
    return [
        Tool(
            "web_search",
            "Search the web for recent information on a query. Returns titles, URLs and snippets.",
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "search query"}},
                "required": ["query"],
            },
        ),
        Tool(
            "fetch_url",
            "Fetch a URL and return its readable text content. Goes through the "
            "restricted gateway, so only allowlisted sites are reachable and "
            "redirects to other destinations are refused.",
            {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
        Tool(
            "shell",
            "Run a shell command inside the agent's environment (the persistent microVM when active).",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["command"],
            },
        ),
        Tool(
            "read_file",
            "Read a text file from the agent's filesystem.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        Tool(
            "write_file",
            "Write content to a file on the agent's filesystem.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        Tool(
            "list_dir",
            "List files and directories at a path.",
            {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        ),
        Tool(
            "browse",
            "Browse a website through the restricted browser. It keeps one session "
            "across calls, so goto, then click/type/scroll act on the same live page. "
            "Only sites the owner has approved on the allowlist can be reached; every "
            "other destination, every non-http(s) scheme, and every private or internal "
            "address is refused by the network gateway. This browser is separate from "
            "the computer-control browser and has no logins or cookies. "
            "Actions: goto (url), text (read visible page text), click (selector), "
            "type (text, optionally enter=true), press (key like ENTER/TAB), scroll (dx/dy), "
            "back, forward, eval (script).",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["goto", "text", "click", "type", "press", "scroll", "back", "forward", "eval"],
                        "description": "what to do",
                    },
                    "url": {"type": "string", "description": "URL for goto; must be an http(s) site on the allowlist"},
                    "selector": {"type": "string", "description": "CSS selector for click/type"},
                    "text": {"type": "string", "description": "text to type"},
                    "enter": {"type": "boolean", "description": "press Enter after typing"},
                    "key": {"type": "string", "description": "key name for press (ENTER, TAB, ESC, ...)"},
                    "dx": {"type": "integer", "description": "horizontal scroll delta"},
                    "dy": {"type": "integer", "description": "vertical scroll delta"},
                    "script": {"type": "string", "description": "JS for eval"},
                    "timeout": {"type": "integer", "default": 45},
                },
                "required": ["action"],
            },
        ),
    ] + routine_tools() + notification_tools()


def commander_tools() -> List[Tool]:
    return worker_tools()