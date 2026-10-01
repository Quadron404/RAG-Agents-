"""The real HTTP path, against a real server that really returns 429.

The other suite substitutes a provider that raises, which proves the runner's
handling but not the request layer's: nothing yet proved that a streamed
response's error body is actually read, that ``Retry-After`` survives, and that
the message says "Provider reached" instead of "could not be reached".  Those
are exactly the parts a stubbed provider cannot check, so this binds a socket
and lets ``openai_compat`` talk to it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.computer.runner import ComputerRunner, ComputerRun
from app.providers.base import LLMMessage
from app.providers.errors import ProviderHTTPError
from app.providers.openai_compat import OpenAICompatProvider

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))


class Handler(BaseHTTPRequestHandler):
    """Answers with whatever ``server.script`` says, then closes."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        self.rfile.read(length)        # drain, or the client sees a reset
        status, headers, body = self.server.script
        raw = body.encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server:
    def __init__(self, script):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.script = script
        self.threads = 0
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/chat/completions"

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


class CountingHandler(Handler):
    """Same, but counts requests so a retry budget can be measured for real."""

    def do_POST(self):
        self.server.threads += 1
        Handler.do_POST(self)


class CountingServer(Server):
    def __init__(self, script):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), CountingHandler)
        self.httpd.script = script
        self.httpd.threads = 0
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)


RATE_LIMIT_BODY = json.dumps(
    {
        "error": {
            "message": "Rate limit reached for mistral-small-2506. Please try again in 2s.",
            "type": "rate_limit_exceeded",
            "code": "429",
        }
    }
)


def provider_for(url, name="mistral"):
    """The real provider, pointed at the test server instead of an API host."""
    base = url.rsplit("/chat/completions", 1)[0]
    return OpenAICompatProvider(
        name=name,
        api_key="test-key-not-a-real-secret",
        base_url=base,
        timeout=20.0,
    )


MODEL = "mistral-small-2506"


class Router:
    def __init__(self, provider):
        self.provider = provider

    def resolve(self, role, provider_name=None):
        return self.provider, MODEL


def runner_for(provider):
    runner = ComputerRunner.__new__(ComputerRunner)
    runner.settings = Settings(computer_max_steps=1)
    runner.settings.computer_max_http_attempts = 1
    runner.router = Router(provider)
    runner.computer = None
    runner._runs = {}
    return runner


def ask(runner):
    run = ComputerRun(task_id="t", task="task")
    return asyncio.get_event_loop().run_until_complete(
        runner._ask([LLMMessage(role="user", content="hi")], run)
    )


def test_real_429_over_the_wire():
    with Server((429, {"content-type": "application/json", "retry-after": "2"}, RATE_LIMIT_BODY)) as srv:
        try:
            ask(runner_for(provider_for(srv.url)))
            check("a real 429 reaches the caller as ProviderHTTPError", False, "no exception")
            return
        except ProviderHTTPError as exc:
            check("a real 429 reaches the caller as ProviderHTTPError", True, type(exc).__name__)
            check("status is read off the wire", exc.status == 429, str(exc.status))
            check("reason phrase is read off the wire",
                  exc.reason.lower().startswith("too many requests"), exc.reason)
            check("Retry-After: 2 is honoured", exc.retry_after == 2.0, repr(exc.retry_after))
            check("the provider's own message is extracted from the JSON body",
                  "Rate limit reached for mistral-small-2506" in exc.provider_detail,
                  exc.provider_detail)
            check("the raw body is kept verbatim", exc.body == RATE_LIMIT_BODY)
            check("the summary says Provider reached, never 'could not be reached'",
                  exc.message.startswith("Provider reached")
                  and "could not be reached" not in exc.message,
                  exc.message)
            check("the summary names the status", "HTTP 429" in exc.message, exc.message)


def test_real_401_is_not_retried_over_the_wire():
    with CountingServer((401, {"content-type": "application/json"},
                         json.dumps({"error": {"message": "Invalid API key"}}))) as srv:
        try:
            ask(runner_for(provider_for(srv.url, "openrouter")))
            check("a real 401 raises", False, "no exception")
        except ProviderHTTPError as exc:
            check("a real 401 raises", True, exc.message)
            check("a real 401 is sent exactly once, no retry storm",
                  srv.httpd.threads == 1, f"{srv.httpd.threads} requests hit the server")


def test_real_503_is_retried_a_bounded_number_of_times_over_the_wire():
    with CountingServer((503, {"content-type": "application/json", "retry-after": "0"},
                         json.dumps({"error": {"message": "upstream is down"}}))) as srv:
        runner = runner_for(provider_for(srv.url))
        runner.settings.computer_max_http_attempts = 3
        try:
            ask(runner)
            check("a real 503 raises after its attempts", False, "no exception")
        except ProviderHTTPError as exc:
            check("a real 503 raises after its attempts", True, exc.message)
            check("a real 503 was re-sent, and only as often as allowed",
                  srv.httpd.threads == 3, f"{srv.httpd.threads} requests hit the server")


def test_real_retry_after_date_form():
    """A date-form Retry-After must be read, not thrown away as unparseable."""
    with Server((429,
                 {"content-type": "application/json",
                  "retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"},
                 RATE_LIMIT_BODY)) as srv:
        try:
            ask(runner_for(provider_for(srv.url)))
            check("date-form Retry-After does not raise", False, "no exception")
        except ProviderHTTPError as exc:
            check("date-form Retry-After does not raise", True, exc.message)
            check("date-form Retry-After is converted to seconds",
                  exc.retry_after is not None and exc.retry_after > 0, repr(exc.retry_after))


def test_real_non_json_error_body_is_still_shown():
    with Server((502, {"content-type": "text/html"}, "<html><body>Bad Gateway</body></html>")) as srv:
        try:
            ask(runner_for(provider_for(srv.url)))
            check("an HTML error page raises", False, "no exception")
        except ProviderHTTPError as exc:
            check("an HTML error page raises", True, exc.message)
            check("a non-JSON body is shown rather than dropped",
                  "Bad Gateway" in exc.provider_detail, exc.provider_detail)


def test_connection_refused_is_not_reported_as_reached():
    """Nothing is listening on this port: nothing was reached, so do not claim it was."""
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()          # bound then released: now genuinely nothing is there
    provider = provider_for(f"http://127.0.0.1:{dead_port}")
    try:
        ask(runner_for(provider))
        check("a refused connection does not become ProviderHTTPError", False, "no exception")
    except ProviderHTTPError as exc:
        check("a refused connection does not become ProviderHTTPError", False,
              f"misreported as reached: {exc.message}")
    except Exception as exc:                    # noqa: BLE001 - the point
        check("a refused connection does not become ProviderHTTPError", True,
              f"{type(exc).__name__}")


def main() -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for fn in [
        test_real_429_over_the_wire,
        test_real_401_is_not_retried_over_the_wire,
        test_real_503_is_retried_a_bounded_number_of_times_over_the_wire,
        test_real_retry_after_date_form,
        test_real_non_json_error_body_is_still_shown,
        test_connection_refused_is_not_reported_as_reached,
    ]:
        print(f"\n--- {fn.__name__} ---")
        fn()
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 60)
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks pass")
    for name, _, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())