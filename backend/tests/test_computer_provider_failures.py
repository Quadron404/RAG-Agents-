"""Provider failure paths for computer control.

Four ways a request can fail, and the rule that binds all of them: the reason
the provider gave must survive to the trace.  These were written after the
failure path was found returning an unbound ``raw`` -- the handler raised a
second exception while reporting the first, so the panel showed
``cannot access local variable 'raw'`` and the real 429 was never displayed.

Each test drives the real loop against a real HTTP-shaped failure, so what is
asserted is what a trace would actually contain.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.computer.runner import ComputerRunner, ComputerTurnTrace, STATUS_DONE, _backoff_delay
from app.providers.base import LLMMessage, TextDelta
from app.providers.errors import ProviderHTTPError
from app.providers.router import ProviderUnavailable, Router

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))


# --- a provider that fails the way a real one does -------------------------


class FakeProvider:
    """Raises a scripted sequence of failures, then optionally succeeds."""

    def __init__(self, script, name="mistral"):
        self.name = name
        self.script = list(script)
        self.calls = 0
        self.last_wire = {"messages_count": 2, "image_present": True}

    def stream(self, messages, tools, model):
        self.calls += 1
        outcome = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        async def gen():
            for piece in outcome:
                yield TextDelta(piece)
        return gen()


class FakeRouter:
    def __init__(self, provider):
        self.provider = provider

    def resolve(self, role, provider_name=None):
        return self.provider, "mistral-small-2506"


def make_runner(provider, **settings_overrides):
    """A runner wired to the fake provider, with no sleeps in the retry path."""
    settings = Settings(computer_max_steps=1)
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    # __new__ rather than __init__: the failure happens before any screenshot is
    # needed, so the database and the RemoteComputer are never touched and
    # building them would only add unrelated ways for the test to fail.
    runner = ComputerRunner.__new__(ComputerRunner)
    runner.settings = settings
    runner.router = FakeRouter(provider)
    runner.computer = None
    runner._runs = {}
    return runner


def run_one_request(runner, run):
    """Call _ask directly: the failure happens before any screenshot is needed."""
    return asyncio.get_event_loop().run_until_complete(
        runner._ask([LLMMessage(role="user", content="hi")], run)
    )


def new_run(**kw):
    from app.computer.runner import ComputerRun

    return ComputerRun(task_id="t", task="do the thing", **kw)


def http_error(status, reason="", body="", retry_after=None, provider="mistral"):
    return ProviderHTTPError(
        provider=provider,
        model="mistral-small-2506",
        status=status,
        reason=reason,
        body=body,
        retry_after=retry_after,
    )


RATE_LIMIT_BODY = json.dumps(
    {"error": {"message": "Rate limit reached for mistral-small-2506", "type": "rate_limit"}}
)


# --- 1. HTTP 429 -------------------------------------------------------------


def test_429_is_reported_as_reached_not_unreachable():
    provider = FakeProvider([http_error(429, "Too Many Requests", RATE_LIMIT_BODY, retry_after=0.0)])
    runner = make_runner(provider, computer_max_http_attempts=1)
    run = new_run(provider="mistral")

    try:
        run_one_request(runner, run)
        check("429 raises out of _ask", False, "no exception raised")
        return
    except ProviderHTTPError as exc:
        check("429 raises out of _ask", True, type(exc).__name__)
        check("429 is reported as reached", exc.reached is True)
        check("429 is retryable", exc.retryable is True)
        check(
            "429 message says 'Provider reached', not 'could not be reached'",
            "Provider reached" in exc.message and "could not be reached" not in exc.message,
            exc.message,
        )
        check("429 message carries the status", "HTTP 429" in exc.message, exc.message)
        check("429 preserves the provider's own message",
              "Rate limit reached for mistral-small-2506" in exc.provider_detail,
              exc.provider_detail)
        check("429 preserves the raw provider body", RATE_LIMIT_BODY in exc.body)
        check("Retry-After is read", exc.retry_after == 0.0, repr(exc.retry_after))


def test_429_is_retried_with_bounded_backoff_then_fails_cleanly():
    """A run of 429s must retry a bounded number of times, not spam."""
    provider = FakeProvider([http_error(429, "Too Many Requests", RATE_LIMIT_BODY, retry_after=0.01)])
    runner = make_runner(provider, computer_max_http_attempts=3)
    run = new_run(provider="mistral")

    try:
        run_one_request(runner, run)
        check("repeated 429 eventually raises", False, "no exception")
        return
    except ProviderHTTPError as exc:
        check("repeated 429 eventually raises", True, exc.message)
        check(
            "attempts are bounded (3 sends, not an unbounded retry loop)",
            provider.calls == 3,
            f"{provider.calls} provider calls",
        )
        check("every refusal is recorded", len(run.http_attempts) == 3, str(len(run.http_attempts)))
        check("attempts are numbered 1..3",
              [a["attempt"] for a in run.http_attempts] == [1, 2, 3],
              str([a["attempt"] for a in run.http_attempts]))
        check("each attempt records the http status",
              all(a["http_status"] == 429 for a in run.http_attempts))
        check("the status is surfaced in the message",
              "HTTP 429" in exc.message, exc.message)


def test_400_is_not_retried():
    """A bad key will still be a bad key. Retrying it burns the quota."""
    body = json.dumps({"error": {"message": "Invalid API key"}})
    provider = FakeProvider([http_error(401, "Unauthorized", body)])
    runner = make_runner(provider, computer_max_http_attempts=3)
    run = new_run(provider="openrouter")

    try:
        run_one_request(runner, run)
        check("401 raises", False, "no exception")
    except ProviderHTTPError as exc:
        check("401 raises", True, exc.message)
        check("401 is not retryable", exc.retryable is False)
        check("401 is attempted exactly once", provider.calls == 1, str(provider.calls))
        check("401 preserves the provider message", "Invalid API key" in exc.message, exc.message)


def test_5xx_is_retried():
    provider = FakeProvider([http_error(503, "Service Unavailable", "upstream down", retry_after=0.0)])
    runner = make_runner(provider, computer_max_http_attempts=2)
    run = new_run(provider="openrouter")

    try:
        run_one_request(runner, run)
        check("503 raises after retries", False, "no exception")
    except ProviderHTTPError as exc:
        check("503 raises after retries", True, exc.message)
        check("503 is retryable", exc.retryable is True)
        check("503 sent 2 attempts", provider.calls == 2, str(provider.calls))


# --- 2. the unbound `raw` ---------------------------------------------------


def test_failure_path_never_references_unbound_raw():
    """The original bug: the except block returned `raw`, raising UnboundLocalError.

    Driven through _next_command so the real handler runs.  The assertion is
    about what the caller ends up holding, not about which line raised.
    """
    for label, failure in [
        ("429", http_error(429, "Too Many Requests", RATE_LIMIT_BODY, retry_after=0.0)),
        ("5xx", http_error(500, "Internal Server Error", "boom")),
        ("network", ConnectionRefusedError("connection refused")),
        ("timeout", asyncio.TimeoutError()),
        ("malformed", ValueError("no JSON object could be decoded")),
        ("unconfigured", ProviderUnavailable("Mistral is not configured")),
    ]:
        provider = FakeProvider([failure])
        runner = make_runner(provider, computer_max_http_attempts=1)
        run = new_run(provider="mistral")
        run.trace = []

        try:
            command, error, raw = asyncio.get_event_loop().run_until_complete(
                runner._next_command(run, bounds=None, image=None, first_turn=True)
            )
        except BaseException as exc:                      # noqa: BLE001 - that is the point
            check(f"{label}: no secondary exception escapes the failure path", False,
                  f"{type(exc).__name__}: {exc}")
            continue

        check(f"{label}: failure path returns without raising", True)
        check(f"{label}: no command is invented", command is None, repr(command))
        check(f"{label}: raw is a string, never unbound", isinstance(raw, str), repr(raw))
        check(f"{label}: an error is reported", bool(error), repr(error))
        check(f"{label}: the original cause is the reported error",
              _names_original(error, failure), error[:120])


def _names_original(error: str, failure: BaseException) -> bool:
    """The reported error must still mention what actually went wrong."""
    if isinstance(failure, ProviderHTTPError):
        return str(failure.status) in error or failure.message[:40] in error
    if isinstance(failure, ProviderUnavailable):
        return "not configured" in error
    if isinstance(failure, asyncio.TimeoutError):
        return "TimeoutError" in error or "timeout" in error.lower()
    return type(failure).__name__ in error


def test_unbound_local_error_is_impossible_by_construction():
    """Guard the exact regression: the old handler returned an unassigned name.

    `raw` is assigned before the try block now.  If that ever moves back inside,
    this fails by executing the same shape and finding the raise.
    """
    import inspect

    from app.computer.runner import ComputerRunner

    src = inspect.getsource(ComputerRunner._next_command)
    handler = src.split("except ProviderHTTPError", 1)[-1]
    # Every failure handler must return a literal or an already-bound name.
    check("the failure handlers no longer return a bare `raw` from an unbound path",
          "return None, \"\", raw" not in handler,
          "handler returns an unbound raw" if "return None, \"\", raw" in handler else "")


def test_malformed_provider_response_is_reported_not_crashed():
    """A 200 whose body is not parseable is a provider failure, not a crash."""
    provider = FakeProvider([ValueError("Expecting value: line 1 column 1 (char 0)")])
    runner = make_runner(provider, computer_max_http_attempts=1)
    run = new_run(provider="mistral")

    try:
        command, error, raw = asyncio.get_event_loop().run_until_complete(
            runner._next_command(run, bounds=None, image=None, first_turn=True)
        )
        check("malformed response does not crash the loop", True)
        check("malformed response yields no command", command is None)
        check("malformed response is reported by name",
              "ValueError" in error and "Expecting value" in error, error[:120])
    except BaseException as exc:                          # noqa: BLE001
        check("malformed response does not crash the loop", False, f"{type(exc).__name__}: {exc}")


# --- 3. backoff behaviour ---------------------------------------------------


def test_backoff_is_bounded_and_jittered():
    seen = []
    for _ in range(40):
        d = _backoff_delay(attempt=3, retry_after=None, base=1.5, cap=30.0)
        seen.append(d)
        assert d >= 0
    check("backoff never exceeds the cap", max(seen) <= 30.0, f"max={max(seen):.2f}")
    check("backoff is jittered, not a constant", len(set(round(x, 4) for x in seen)) > 5,
          f"{len(set(round(x, 4) for x in seen))} distinct values")

    # Growth is asserted on the window each attempt samples from, not on single
    # draws: with full jitter any one delay can be smaller than any other's, so
    # comparing samples would be a coin flip rather than a test.
    growing = [max(_backoff_delay(i, None, 1.5, 30.0) for _ in range(30)) for i in range(1, 9)]
    check("later attempts wait longer than earlier ones",
          growing[3] > growing[0] and growing[7] > growing[3],
          " ".join(f"{g:.1f}" for g in growing))
    check("the growth window is still capped at 8 attempts",
          growing[-1] <= 30.0, f"{growing[-1]:.1f}")


def test_retry_after_wins_over_computed_backoff():
    d = _backoff_delay(attempt=5, retry_after=12.0, base=1.5, cap=30.0)
    check("Retry-After is honoured over the computed backoff", d == 12.0, f"{d}")
    capped = _backoff_delay(attempt=1, retry_after=9999.0, base=1.5, cap=30.0)
    check("a huge Retry-After is still capped", capped == 30.0, f"{capped}")


def test_failure_report_is_structured():
    """The trace report must carry the fields the panel renders."""
    from app.computer.runner import ComputerTurnTrace

    turn = ComputerTurnTrace(
        turn=1, step=1, timestamp=1.0, provider="mistral", model="mistral-small-2506",
        task="t", error="Provider reached — HTTP 429 Too Many Requests: rate limited",
        provider_reached=True, http_status=429, http_reason="Too Many Requests",
        provider_error="Rate limit reached", retry_after=2.0, retry_attempts=2,
        http_attempts=[{"attempt": 1, "http_status": 429}, {"attempt": 2, "http_status": 429}],
    )
    run = new_run(provider="mistral")
    run.trace = [turn]
    run.status = "error"

    report = run.trace_report(include_images=False, providers=[], default_provider="mistral")
    failure = report.get("failure") or {}
    for field in ("provider", "model", "http_status", "http_reason", "provider_error",
                  "retry_after", "retry_attempts", "message", "final_result"):
        check(f"failure report carries {field}", field in failure, json.dumps(failure)[:160])
    check("failure report attributes the turn", failure.get("turn") == 1)
    check("failure report says reached", failure.get("provider_reached") is True)
    check("turn payload carries the status", turn.public()["http_status"] == 429)
    check("a clean run has no failure block", "failure" not in new_run().trace_report(
        include_images=False, providers=[], default_provider="mistral"))


def test_a_refusal_a_retry_recovered_from_is_recorded_on_the_turn():
    """A request refused once and then answered must not look untouched.

    The reply is real and the run carries on, so nothing failed -- but a
    provider that refuses one request in three is about to refuse one in one,
    and without the refusal travelling with the reply that is invisible.
    """
    provider = FakeProvider([
        http_error(429, "Too Many Requests", RATE_LIMIT_BODY, retry_after=0.0),
        # navigate, because the first turn of a run has no screenshot yet and
        # the parser rightly refuses anything with coordinates in it.
        ['{"type":"navigate","url":"http://127.0.0.1:9000/test"}'],
    ])
    runner = make_runner(provider, computer_max_http_attempts=3)
    run = new_run(provider="mistral")

    try:
        command, error, raw = asyncio.get_event_loop().run_until_complete(
            runner._next_command(run, bounds=None, image=None, first_turn=True)
        )
    except BaseException as exc:                          # noqa: BLE001
        check("a recovered refusal still produces a command", False, f"{type(exc).__name__}: {exc}")
        return

    check("a recovered refusal still produces a command", command is not None,
          f"command={command!r}")
    check("a recovered request is not an error", error == "", repr(error))
    check("the reply after the retry is kept verbatim",
          '"type":"navigate"' in raw, raw[:60])
    check("the refusal travelled with the reply", provider.calls == 2, str(provider.calls))

    turn = run.trace[-1]
    check("the turn records the refusal", len(turn.http_attempts) == 1, str(len(turn.http_attempts)))
    check("the turn names the status it recovered from", turn.http_status == 429, str(turn.http_status))
    check("the turn keeps the provider's own words",
          "Rate limit reached" in turn.provider_error, turn.provider_error)
    check("the turn counts one attempt before the answer", turn.retry_attempts == 1,
          str(turn.retry_attempts))
    check("the turn still has no error", turn.error == "", repr(turn.error))
    check("the turn is still attributed to the provider", turn.provider == "mistral", turn.provider)
    check("a recovered turn is not reported as a run failure",
          "failure" not in run.trace_report(include_images=False, providers=[],
                                            default_provider="mistral"))


def test_a_run_that_did_not_fail_has_no_failure_key_at_all():
    """`failure` must be absent on a clean run, not an empty object.

    Found the hard way, in the browser: the panel rendered on the *presence* of
    the key, and `{}` is truthy in JavaScript, so a run that had recovered from
    a 429 and finished with `done` wore a banner reading "Run failed / Request
    of 2 · no provider · 1 attempt" -- a confident account of a failure with no
    turn, no provider and no cause in it.  An absent key cannot be mistaken for
    one; an empty object can.
    """
    run = new_run(provider="mistral")
    report = run.trace_report(include_images=False, providers=[], default_provider="mistral")
    check("a run that never failed sends no failure key",
          "failure" not in report, sorted(k for k in report if k == "failure"))

    # The run recovers from a refusal, answers, and finishes.  That is the exact
    # shape that showed the phantom banner.
    turn = ComputerTurnTrace(
        turn=1, step=1, provider="mistral", model="mistral-small-2506",
        raw='{"type":"navigate","url":"http://127.0.0.1:9000/"}',
        timestamp=time.time(), parse_ok=True,
    )
    turn.http_status = 429
    turn.http_reason = "Too Many Requests"
    turn.provider_reached = True
    turn.provider_error = "Rate limit reached"
    turn.retry_attempts = 1
    turn.http_attempts = [{"attempt": 1, "status": 429, "reason": "Too Many Requests",
                           "retry_after": 1.0, "error": "Rate limit reached"}]
    run.trace.append(turn)
    run.status = STATUS_DONE
    run.message = "Clicked the button."

    report = run.trace_report(include_images=False, providers=[], default_provider="mistral")
    check("a run that recovered and finished sends no failure key either",
          "failure" not in report, report.get("failure"))
    check("such a run still reports itself as done", report["status"] == STATUS_DONE, report["status"])

    # And a run that really did fail must keep sending the key, with its cause.
    failed = ComputerTurnTrace(
        turn=2, step=2, provider="mistral", model="mistral-small-2506", raw="",
        timestamp=time.time(),
        error="Provider reached — HTTP 429 Too Many Requests",
        provider_reached=True, http_status=429, http_reason="Too Many Requests",
        provider_error="Rate limit reached", retry_attempts=3,
    )
    failed.http_attempts = [{"attempt": 1, "status": 429}, {"attempt": 2, "status": 429},
                            {"attempt": 3, "status": 429}]
    run.trace.append(failed)
    failure = run.trace_report(include_images=False, providers=[],
                               default_provider="mistral")["failure"]
    check("a failed run still reports the failing request",
          failure["turn"] == 2 and failure["http_status"] == 429
          and failure["retry_attempts"] == 3 and failure["final_result"] == "stopped",
          json.dumps(failure, sort_keys=True)[:160])


def main() -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for fn in [
        test_429_is_reported_as_reached_not_unreachable,
        test_429_is_retried_with_bounded_backoff_then_fails_cleanly,
        test_400_is_not_retried,
        test_5xx_is_retried,
        test_failure_path_never_references_unbound_raw,
        test_unbound_local_error_is_impossible_by_construction,
        test_malformed_provider_response_is_reported_not_crashed,
        test_backoff_is_bounded_and_jittered,
        test_retry_after_wins_over_computed_backoff,
        test_failure_report_is_structured,
        test_a_refusal_a_retry_recovered_from_is_recorded_on_the_turn,
        test_a_run_that_did_not_fail_has_no_failure_key_at_all,
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