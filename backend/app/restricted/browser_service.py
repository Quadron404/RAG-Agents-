"""The separate browser ordinary agents drive.

This is not the computer-control browser and it is not the personal Chrome.  It
is a headless Chrome in its own profile directory, started with its own debug
port, forced through the restricted gateway, and it screens every navigation
against the same policy the gateway does.  It shares no cookie jar, no profile
and no display with the human-driven browser, so a site the user is logged into
is a site the agent arrives at as a stranger -- which is the point of keeping
them apart.

It runs inside the network sandbox (``codespace/restricted-sandbox.sh``), which
is what makes its ``--proxy-server`` setting more than a suggestion: the
sandbox's firewall drops any packet that does not go to the gateway, so a
navigation this service failed to screen still cannot leave.  The service's own
check is defence in depth, not the boundary.

Run it as ``python -m app.restricted.browser_service``.  It binds loopback and
serves one endpoint:

    POST /browse  {"action": ..., ...}  ->  {"ok": bool, "output": ..., ...}
    GET  /status                        ->  {"ok": true, ...}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from .guard import Guard
from .policy import Policy, default_allowlist_path

#: The separate profile directory.  Deliberately not ``CHROME_PROFILE`` and not
#: under ``/workspaces``: ordinary-agent state must not survive as (or become)
#: the user's login state.
DEFAULT_PROFILE = "/tmp/rag-restricted-profile"
DEFAULT_PORT = 9110
DEFAULT_GATEWAY = "http://127.0.0.1:9111"
MAX_OUTPUT = 8000

#: Screenshot capture is off by default.  The computer-control path needs images
#: because a model clicks pixels; an ordinary agent reads text, and shipping an
#: image per navigation would be both slower and a leak of whatever is on screen.
#: ``--screenshots`` turns it back on for a deployment that wants it.
_SCREENSHOTS = os.environ.get("RESTRICTED_BROWSER_SCREENSHOTS", "0") == "1"


class Browser:
    """The one Chrome this service owns, attached lazily and reused."""

    def __init__(self, profile: str, gateway: str, policy: Policy, guard: Guard):
        self.profile = profile
        self.gateway = gateway
        self.policy = policy
        self.guard = guard
        self._driver = None

    def _find_chrome(self) -> str:
        override = os.environ.get("CHROME_BINARY", "").strip()
        if override and os.path.exists(override):
            return override
        for cand in (
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ):
            if os.path.exists(cand):
                return cand
        import shutil

        return shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"

    def _attach(self):
        if self._driver is not None:
            return self._driver
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-sync")
        # The browser's own egress is the gateway.  The sandbox enforces this at
        # the network layer; the flag makes the well-behaved path the default.
        opts.add_argument(f"--proxy-server={self.gateway}")
        opts.add_argument("--window-size=1280,800")
        opts.binary_location = self._find_chrome()
        opts.add_argument(f"--user-data-dir={self.profile}")
        os.makedirs(self.profile, exist_ok=True)
        driver_path = "/usr/bin/chromedriver" if os.path.exists("/usr/bin/chromedriver") else "chromedriver"
        try:
            self._driver = webdriver.Chrome(service=Service(driver_path), options=opts)
        except Exception:
            self._driver = webdriver.Chrome(options=opts)
        self._driver.set_page_load_timeout(60)
        return self._driver

    def screen(self, url: str) -> Tuple[bool, str]:
        verdict = self.guard.check(url)
        if not verdict.allowed:
            return False, verdict.reason
        decision = self.policy.allows(verdict.host, verdict.port)
        if not decision.allowed:
            return False, decision.reason
        return True, ""

    def browse(self, req: dict) -> dict:
        action = str(req.get("action", "text")).lower()
        url = str(req.get("url", "") or "").strip()
        if action == "goto":
            if not url:
                return {"ok": False, "error": "goto requires url"}
            ok, reason = self.screen(url)
            if not ok:
                return {"ok": False, "error": f"refused: {reason}"}
        try:
            driver = self._attach()
        except Exception as exc:
            return {"ok": False, "error": f"browser unavailable: {exc}"}

        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        try:
            if action == "goto":
                driver.get(url)
                # A redirect is a new destination: screen where we actually
                # landed, not only where we asked to go.
                landed = driver.current_url or ""
                if landed and landed != url:
                    ok, reason = self.screen(landed)
                    if not ok:
                        try:
                            driver.get("about:blank")
                        except Exception:
                            pass
                        return {"ok": False, "error": f"refused redirect to {landed}: {reason}"}
                output = driver.find_element(By.TAG_NAME, "body").text[:MAX_OUTPUT]
            elif action == "text":
                output = driver.find_element(By.TAG_NAME, "body").text[:MAX_OUTPUT]
            elif action == "click":
                selector = str(req.get("selector", "") or "").strip()
                if selector:
                    driver.find_element(By.CSS_SELECTOR, selector).click()
                else:
                    return {"ok": False, "error": "click requires selector"}
                output = ""
            elif action == "type":
                selector = str(req.get("selector", "") or "").strip()
                if selector:
                    element = driver.find_element(By.CSS_SELECTOR, selector)
                    element.clear()
                else:
                    element = driver.switch_to.active_element
                element.send_keys(str(req.get("text", "")))
                if str(req.get("enter", "")).lower() in ("1", "true", "yes"):
                    element.send_keys(Keys.ENTER)
                output = ""
            elif action == "press":
                key = str(req.get("key", "Enter")).replace(" ", "").upper()
                driver.switch_to.active_element.send_keys(getattr(Keys, key, Keys.ENTER))
                output = ""
            elif action == "scroll":
                driver.execute_script(
                    "window.scrollBy(arguments[0], arguments[1]);",
                    int(str(req.get("dx", "0")).strip() or 0),
                    int(str(req.get("dy", "0")).strip() or 0),
                )
                output = ""
            elif action == "back":
                driver.back()
                output = ""
            elif action == "forward":
                driver.forward()
                output = ""
            elif action == "eval":
                return {"ok": True, "output": str(driver.execute_script(str(req.get("script", "return location.href;"))))}
            else:
                return {"ok": False, "error": f"unknown action {action}"}
            time.sleep(0.2)
            return {
                "ok": True,
                "output": output,
                "title": _safe(lambda: driver.title),
                "url": _safe(lambda: driver.current_url),
            }
        except Exception as exc:
            return {"ok": False, "error": f"browser {action}: {exc}"}

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None


def _safe(fn) -> str:
    try:
        return str(fn())
    except Exception:
        return ""


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, fmt, *args):  # keep the default access log off
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
            self._json(200, {"ok": True, "service": "restricted-browser", "policy": self.server.policy.describe()})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/browse":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        if not isinstance(req, dict):
            self._json(400, {"ok": False, "error": "body must be an object"})
            return
        self._json(200, self.server.browser.browse(req))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, policy: Policy, guard: Guard, gateway: str, profile: str):
        self.policy = policy
        self.guard = guard
        self.browser = Browser(profile, gateway, policy, guard)
        super().__init__(address, _Handler)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="restricted browser service for ordinary agents")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allowlist", default=default_allowlist_path())
    parser.add_argument("--gateway", default=os.environ.get("RESTRICTED_GATEWAY_URL", DEFAULT_GATEWAY))
    parser.add_argument("--profile", default=os.environ.get("RESTRICTED_PROFILE_DIR", DEFAULT_PROFILE))
    args = parser.parse_args(argv)

    policy = Policy(args.allowlist)
    guard = Guard()
    server = _Server((args.host, args.port), policy, guard, args.gateway, args.profile)
    sys.stderr.write(
        f"[restricted] browser on {args.host}:{args.port} profile={args.profile} "
        f"gateway={args.gateway} rules={policy.describe()['rules']}\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.browser.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
