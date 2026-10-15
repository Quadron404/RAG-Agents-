"""What the ordinary-agent tools talk to for web access.

Every network path an ordinary agent has is funnelled through this object, and
this object refuses anything the allowlist or the guard rejects.  It is the
seam between the tools (which the agent drives) and the boundary (which the
agent cannot): the tools call it and report what it says, and there is no method
here that widens the policy.

Three network tools and one shell:

``browse``
    Drives the *separate* browser service.  The URL is screened here before the
    request leaves and screened again inside the service, which is itself behind
    the gateway -- three checks, none of them the boundary on its own.
``fetch``
    Fetches a URL as text, following redirects by re-screening each hop, through
    the gateway.  A redirect to a destination nobody approved is a refusal, not
    a fetch.
``search``
    The one search endpoint, reached through the same gateway and therefore
    subject to the same allowlist.
``run_shell``
    Refuses unless a sandboxed shell is deployed.  A shell on the unrestricted
    host is a `curl` away from every destination, so the honest answer when the
    sandbox is absent is "no", not "run it anyway".
"""

from __future__ import annotations

import html
import re
from typing import Dict, Optional
from urllib.parse import urljoin

import httpx

from .guard import Guard
from .policy import Decision, Policy, load_policy

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) RAG-Agents-Restricted/1.0"
_MAX_REDIRECTS = 5


def _result(output: str = "", error: str = "", **extra) -> Dict[str, object]:
    event: Dict[str, object] = {"type": "result", "output": output, "error": error}
    event.update(extra)
    return event


class RestrictedRuntime:
    def __init__(self, settings, *, guard: Optional[Guard] = None, policy: Optional[Policy] = None):
        self.settings = settings
        self.policy = policy or load_policy(settings)
        self.guard = guard or Guard()
        self.gateway_url = getattr(settings, "restricted_gateway_url", "http://127.0.0.1:9111")
        self.browser_url = getattr(settings, "restricted_browser_url", "http://127.0.0.1:9110")
        self.shell_url = (getattr(settings, "restricted_shell_url", "") or "").rstrip("/")

    @property
    def enabled(self) -> bool:
        return settings_bool(self.settings, "restricted_enabled")

    def describe(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "gateway": self.gateway_url,
            "browser": self.browser_url,
            "shell": self.shell_url or "(not deployed)",
            "policy": self.policy.describe(),
        }

    def screen(self, url: str) -> Decision:
        """The one place a URL is both shaped-checked and policy-checked."""
        verdict = self.guard.check(url)
        if not verdict.allowed:
            return Decision(False, verdict.reason, verdict.host, verdict.port)
        return self.policy.allows(verdict.host, verdict.port)

    # --- browser ----------------------------------------------------------
    async def browse(self, args: Dict[str, object]) -> Dict[str, object]:
        action = str(args.get("action", "text")).lower()
        url = str(args.get("url", "") or "").strip()
        if action == "goto":
            if not url:
                return _result(error="goto requires url")
            decision = self.screen(url)
            if not decision.allowed:
                return _result(error=f"refused: {decision.reason}")
        payload = dict(args)
        payload["action"] = action
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(f"{self.browser_url}/browse", json=payload)
            if resp.status_code != 200:
                return _result(error=f"restricted browser returned {resp.status_code}")
            data = resp.json()
        except Exception as exc:
            # Fail closed: an unreachable browser service is a refusal, never a
            # quiet fallback to the personal browser.
            return _result(error=f"restricted browser unavailable: {exc}")
        return _result(
            output=str(data.get("output", "")),
            error=str(data.get("error", "")),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
        )

    # --- fetch ------------------------------------------------------------
    async def fetch(self, url: str) -> Dict[str, object]:
        current = (url or "").strip()
        for _ in range(_MAX_REDIRECTS):
            decision = self.screen(current)
            if not decision.allowed:
                return _result(error=f"refused: {decision.reason}")
            try:
                status, headers, body = await self._get_through_gateway(current)
            except Exception as exc:
                return _result(error=f"fetch failed: {exc}")
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location", "")
                if not location:
                    return _result(error="redirect with no Location")
                nxt = urljoin(current, location)
                next_decision = self.guard.check_redirect(current, nxt)
                if not next_decision.allowed:
                    return _result(error=f"refused redirect to {nxt}: {next_decision.reason}")
                current = nxt
                continue
            if status >= 400:
                return _result(error=f"http {status}")
            text = _readable_text(body)
            if not text:
                return _result(error="page produced no readable text")
            limit = int(getattr(self.settings, "max_web_chars", 12000))
            return _result(output=text[:limit], url=current)
        return _result(error="too many redirects")

    async def _get_through_gateway(self, url: str):
        async with httpx.AsyncClient(
            proxy=self.gateway_url,
            timeout=30.0,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=False,
        ) as client:
            resp = await client.get(url)
            return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content

    # --- search -----------------------------------------------------------
    async def search(self, query: str) -> Dict[str, object]:
        url = "https://api.duckduckgo.com/"
        params = {"q": query.strip(), "format": "json", "no_html": 1, "skip_disambig": 1}
        decision = self.screen(url)
        if not decision.allowed:
            return _result(error=f"refused: {decision.reason}")
        try:
            async with httpx.AsyncClient(
                proxy=self.gateway_url,
                timeout=30.0,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                resp = await client.get(url, params=params)
            data = resp.json()
        except Exception as exc:
            return _result(error=f"search failed: {exc}")
        results = []
        abstract = str(data.get("AbstractText", "")).strip()
        if data.get("AbstractURL"):
            results.append({"title": data.get("Heading", "Abstract"), "url": data["AbstractURL"], "snippet": abstract})
        for topic in _flatten(data.get("RelatedTopics") or []):
            if "Text" in topic and "FirstURL" in topic:
                results.append({"title": topic.get("Text", "")[:80], "url": topic["FirstURL"], "snippet": topic.get("Text", "")})
        if not results:
            return _result(error="no results returned (DuckDuckGo instant answer)")
        limit = int(getattr(self.settings, "max_web_chars", 12000))
        text = "Search results:\n" + "\n".join(
            f"- {r['title']}: {r['url']}\n  {r['snippet'][:200]}" for r in results[:6]
        )
        return _result(output=text[:limit])

    # --- shell ------------------------------------------------------------
    async def run_shell(self, command: str, timeout: int = 30) -> Dict[str, object]:
        if not command.strip():
            return _result(error="empty command")
        if not self.shell_url:
            # The refusal is the point: running this on the host would make the
            # gateway optional, which is the one thing it must never be.
            return _result(
                error="restricted shell is not deployed; ordinary-agent shell is disabled "
                "while restricted browsing is on"
            )
        try:
            async with httpx.AsyncClient(timeout=max(timeout, 30) + 10.0) as client:
                resp = await client.post(
                    f"{self.shell_url}/shell", json={"command": command, "timeout": timeout}
                )
            if resp.status_code != 200:
                return _result(error=f"restricted shell returned {resp.status_code}")
            data = resp.json()
        except Exception as exc:
            return _result(error=f"restricted shell unavailable: {exc}")
        return _result(output=str(data.get("output", "")), error=str(data.get("error", "")))


def settings_bool(settings, name: str) -> bool:
    """Read a boolean setting whether it is a real bool or a raw string."""
    value = getattr(settings, name, False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _readable_text(body: bytes) -> str:
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return ""
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _flatten(topics) -> list:
    out = []
    for t in topics:
        if isinstance(t, list):
            out.extend(_flatten(t))
        elif isinstance(t, dict):
            out.append(t)
    return out
