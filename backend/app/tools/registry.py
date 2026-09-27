from __future__ import annotations

import json
from typing import List

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
            "Fetch a URL and return its readable text content.",
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
            "browser",
            "Control the agent's persistent browser inside the microVM. "
            "It retains one session across calls, so goto, then click/type/scroll all act on the same live page. "
            "Actions: goto (url), text (read visible page text), click (selector or x/y pixel coords in the 1280x800 viewport), "
            "type (text, optionally with enter=true), press (key like ENTER/TAB), scroll (dx/dy), back, forward, eval (script). "
            "Every action returns a fresh screenshot image plus the page title/url.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["goto", "text", "click", "type", "press", "scroll", "back", "forward", "eval"],
                        "description": "what to do",
                    },
                    "url": {"type": "string", "description": "URL for goto"},
                    "selector": {"type": "string", "description": "CSS selector for click/type"},
                    "x": {"type": "integer", "description": "x pixel in 1280-wide viewport"},
                    "y": {"type": "integer", "description": "y pixel in 800-tall viewport"},
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
    ]


def commander_tools() -> List[Tool]:
    return worker_tools()