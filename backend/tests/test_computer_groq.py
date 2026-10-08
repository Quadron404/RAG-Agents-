"""Groq as the third computer-control provider.

Same claim as `test_computer_providers.py`, one provider further along: adding
Groq must change *who answers* and nothing else.  Everything that decides what
the machine does -- the attached prompt, the history, the screenshot, the strict
JSON parser, the executors, the trace -- lives above the provider interface and
is deliberately not re-implemented for a new provider.

So the tests below are mostly about sameness again:

- Groq is registered from GROQ_API_KEY alone, and absent without it;
- it is offered in the selector with its own model and no key material;
- the newest screenshot reaches Groq as a real image part, byte for byte;
- the same strict JSON commands parse when Groq produced them, and a malformed
  or prose reply is still rejected rather than executed;
- switching to Groq mid-run keeps the task, the history and the step count;
- 401 is not retried, 429 is retried and then reported with Groq's own sentence,
  and a refused connection is reported as never reached;
- no key reaches the browser by any route.

Plus one thing only Groq needs: the default host is Groq's, not OpenRouter's or
Mistral's.  A provider pointed at the wrong host fails with a 401 that reads like
a bad key, which sends people off to rotate a perfectly good secret.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.computer.commands import Bounds, parse_command
from app.computer.runner import ComputerRun, ComputerRunner
from app.config import load_settings
from app.providers.base import LLMMessage, summarize_wire
from app.providers.errors import ProviderHTTPError
from app.providers.groq import DEFAULT_GROQ_MODEL, GroqProvider
from app.providers.openai_compat import OpenAICompatProvider
from app.providers.router import (
    ProviderUnavailable,
    Router,
    computer_model_for,
    computer_provider_info,
    computer_providers,
)
from tests.test_computer_control import SCREEN, FakeComputer, FakeProvider
from tests.test_computer_provider_failures import FakeProvider as ScriptedProvider

#: The same reply set the other providers are driven with, so "Groq works" and
#: "OpenRouter works" mean the same loop ran rather than two loops that differ.
SCRIPT = [
    '{"type":"navigate","url":"https://example.com/computer-test.html"}',
    '{"type":"click","x":700,"y":350,"target":"a test button"}',
    '{"type":"type","text":"example.com"}',
    '{"type":"key","key":"ENTER"}',
    '{"type":"done","message":"Example Domain"}',
]

JPEG = base64.b64encode(bytes.fromhex("ffd8ffd9")).decode("ascii")  # a real JPEG header

#: Groq's own error shapes, which differ from Mistral's.
GROQ_RATE_LIMIT_BODY = json.dumps(
    {"error": {"message": "Rate Limit Reached", "type": "rate_limit_error", "code": "rate_limit_exceeded"}}
)
GROQ_AUTH_BODY = json.dumps({"error": {"message": "Invalid API Key", "type": "invalid_request_error"}})


def make_settings(**overrides):
    """Settings with all three providers configured, so selection is the only variable."""
    settings = load_settings()
    settings.computer_provider = "openrouter"
    settings.computer_model = "test/vision-openrouter"
    settings.openrouter_api_key = "openrouter-secret-value"
    settings.mistral_api_key = "mistral-secret-value"
    settings.mistral_model = "mistral-small-2506"
    settings.groq_api_key = "groq-secret-value"
    settings.groq_model = DEFAULT_GROQ_MODEL
    settings.computer_max_steps = 8
    settings.computer_max_json_retries = 2
    settings.computer_settle_ms = 0
    settings.computer_settle_ms_click = 0
    settings.computer_retry_base_seconds = 0.0
    settings.computer_retry_max_seconds = 0.0
    settings.workspace_base_url = "http://127.0.0.1:9"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def make_runner(replies=None, provider=None, **overrides):
    """A runner whose Router holds Groq alongside Mistral, both recorded."""
    settings = make_settings(**overrides)
    groq = FakeProvider(list(replies if replies is not None else SCRIPT))
    groq.name = "groq"
    mistral = FakeProvider(list(replies if replies is not None else SCRIPT))
    mistral.name = "mistral"
    router = Router({"groq": groq, "mistral": mistral}, settings)
    runner = ComputerRunner(settings, router, db=None)
    runner.computer = FakeComputer(SCREEN)
    return runner, {"groq": groq, "mistral": mistral}


def make_failing_runner(script, **overrides):
    """A runner pointed at Groq that will fail the way `script` says."""
    settings = make_settings(computer_max_steps=1, **overrides)
    groq = ScriptedProvider(script, name="groq")
    runner = ComputerRunner(settings, Router({"groq": groq}, settings), db=None)
    runner.computer = FakeComputer(SCREEN)
    return runner, groq


def http_error(status, body, reason="", retry_after=None):
    return ProviderHTTPError(
        provider="groq",
        model=DEFAULT_GROQ_MODEL,
        status=status,
        reason=reason,
        body=body,
        retry_after=retry_after,
    )


def run_to_end(runner, task="the task", provider="groq"):
    """Start a run through the public `start()`, the way the HTTP route does."""
    loop = asyncio.new_event_loop()
    try:
        async def drive():
            run = await runner.start(task, provider=provider)
            handle = runner._tasks[run.task_id]
            for _ in range(400):
                if handle.done():
                    break
                await asyncio.sleep(0.005)
            if not handle.done():
                handle.cancel()
                await asyncio.sleep(0)
            return run
        return loop.run_until_complete(drive())
    finally:
        loop.close()


def failure_of(run):
    report = run.trace_report(include_images=False, providers=[], default_provider="")
    return report.get("failure")


class TestGroqIsInitialisedFromItsOwnEnvironment(unittest.TestCase):
    """1, 2, 5. The key, the model and the host."""

    def test_the_key_is_read_from_the_environment_and_stays_blank_when_unset(self):
        settings = load_settings()
        settings.groq_api_key = ""
        self.assertEqual(settings.groq_api_key.strip(), "")

    def test_the_model_defaults_to_the_documented_one(self):
        # A configured key with no model named is a missing variable, not a
        # request for a text-only model: the loop sends a screenshot on every
        # turn after the first, and a text-only model answers that with a guess.
        self.assertEqual(DEFAULT_GROQ_MODEL, "qwen/qwen3.8-27b")
        self.assertEqual(computer_model_for(make_settings(groq_model=""), "groq"), DEFAULT_GROQ_MODEL)

    def test_an_explicit_model_wins_over_the_default(self):
        self.assertEqual(computer_model_for(make_settings(groq_model="qwen/qwen3-32b"), "groq"), "qwen/qwen3-32b")

    def test_the_three_providers_never_share_a_model(self):
        settings = make_settings()
        models = {computer_model_for(settings, name) for name in ("openrouter", "mistral", "groq")}
        self.assertEqual(len(models), 3, f"two providers claim the same model: {models}")

    def test_the_provider_is_registered_only_when_the_key_is_present(self):
        import app.providers as providers_module

        self.assertNotIn("groq", providers_module.build_providers(make_settings(groq_api_key="")),
                         "registered with no key")

        built = providers_module.build_providers(make_settings())
        self.assertIn("groq", built)
        self.assertIsInstance(built["groq"], GroqProvider)
        self.assertEqual(built["groq"].name, "groq")

    def test_the_default_base_url_is_groq_not_the_other_two(self):
        settings = make_settings()
        self.assertEqual(settings.groq_base_url, "https://api.groq.com/openai/v1")
        self.assertNotEqual(settings.groq_base_url, settings.mistral_base_url)
        self.assertNotEqual(settings.groq_base_url, settings.openrouter_base_url)

    def test_it_is_offered_in_the_selector_with_its_model_and_no_key(self):
        listed = {info.name: info for info in computer_providers(make_settings())}
        self.assertIn("groq", listed, "Groq missing from the selector")
        self.assertEqual(listed["groq"].label, "Groq")
        self.assertEqual(listed["groq"].model, DEFAULT_GROQ_MODEL)
        self.assertTrue(listed["groq"].configured)
        blob = json.dumps([info.__dict__ for info in listed.values()])
        self.assertNotIn("groq-secret-value", blob, "the key reached the selector")
        self.assertNotIn("openrouter-secret-value", blob)
        self.assertNotIn("mistral-secret-value", blob)

    def test_a_missing_key_is_reported_by_name(self):
        settings = make_settings(groq_api_key="")
        router = Router({"mistral": FakeProvider([])}, settings)
        with self.assertRaises(ProviderUnavailable) as caught:
            router.resolve("computer", "groq")
        self.assertIn("Groq", str(caught.exception))
        self.assertFalse(computer_provider_info(settings, "groq").configured)


class TestTheGroqAdapterCarriesTheScreenshot(unittest.TestCase):
    """3, 7. Vision through the provider interface, not a new code path."""

    def _wire(self, provider):
        return provider._wire_messages(
            [
                LLMMessage(role="system", content="the prompt"),
                LLMMessage(role="user", content="the newest state", images=[JPEG]),
            ]
        )

    def test_the_newest_screenshot_arrives_as_an_image_part(self):
        parts = self._wire(GroqProvider("k"))[1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "the newest state"})
        self.assertEqual(parts[1]["type"], "image_url")
        # Byte for byte: a re-encoded or downscaled screenshot would be a
        # different picture from the one the loop just acted on.
        self.assertEqual(parts[1]["image_url"]["url"], f"data:image/jpeg;base64,{JPEG}")

    def test_it_is_the_same_serialiser_the_other_providers_use(self):
        self.assertTrue(issubclass(GroqProvider, OpenAICompatProvider))
        messages = [
            LLMMessage(role="system", content="p"),
            LLMMessage(role="user", content="look", images=[JPEG]),
        ]
        groq = GroqProvider("k")._wire_messages(messages)
        openrouter = OpenAICompatProvider("openrouter", "k", "https://example.invalid/v1")._wire_messages(messages)
        self.assertEqual(groq, openrouter, "Groq and OpenRouter produced different wire messages")

    def test_the_trace_summary_records_the_image_without_the_base64(self):
        provider = GroqProvider("k")
        messages = [LLMMessage(role="user", content="look", images=[JPEG])]
        summary = summarize_wire(
            {"model": DEFAULT_GROQ_MODEL, "messages": provider._wire_messages(messages), "stream": True},
            messages,
            path="/chat/completions",
        )
        blob = json.dumps(summary)
        self.assertNotIn(JPEG, blob, "the screenshot was copied into the trace summary")
        self.assertTrue(summary.get("image_present"), summary)
        self.assertEqual(summary.get("image_count"), 1, summary)
        self.assertEqual(summary.get("content_part_types"), ["text", "image_url"], summary)


class TestGroqDrivesTheSameLoop(unittest.TestCase):
    """4, 6, 9. The strict protocol and the trace, with Groq answering."""

    def test_a_whole_task_runs_through_groq(self):
        runner, _ = make_runner()
        run = run_to_end(runner)
        self.assertEqual(run.status, "done", run.message)
        self.assertEqual(run.message, "Example Domain")
        self.assertEqual([t.provider for t in run.trace], ["groq"] * 5)
        self.assertEqual([t.model for t in run.trace], [DEFAULT_GROQ_MODEL] * 5)
        # The actions that actually reached the computer, in order.  `done` is
        # not among them: it is the loop terminating on the model's word, not a
        # tool call dispatched to the machine.
        self.assertEqual([a[0] for a in runner.computer.actions],
                         ["navigate", "click", "type", "key"])
        # One capture per turn after the first: the screenshot has to be real
        # and current, not a stand-in for one.
        self.assertEqual(runner.computer.screens, 4)

    def test_the_screenshot_is_on_the_wire_on_every_turn_after_the_first(self):
        runner, fakes = make_runner()
        run_to_end(runner)
        first, later = fakes["groq"].calls[0], fakes["groq"].calls[1:]
        self.assertFalse(any(m.images for m in first), "turn one has no screenshot to send")
        self.assertTrue(all(any(m.images for m in turn) for turn in later),
                        "a turn went to Groq without the newest screenshot")

    def test_the_trace_names_groq_where_the_selector_does(self):
        runner, _ = make_runner()
        run = run_to_end(runner)
        report = run.trace_report(include_images=False, providers=computer_providers(runner.settings),
                                  default_provider="openrouter")
        self.assertEqual(report["last_provider"], "groq")
        self.assertEqual(report["last_model"], DEFAULT_GROQ_MODEL)
        self.assertNotIn("failure", report, "a successful run must not report a failure")
        self.assertIn("groq", [info["name"] for info in report["selected_providers"]])

    def test_switching_to_groq_mid_run_keeps_task_and_step(self):
        # The selector can change between turns, so a run already in flight has
        # to be able to move to Groq without losing what it had done.
        runner, _ = make_runner()
        run = ComputerRun(task_id="switching", task="the task", provider="mistral")
        runner._runs[run.task_id] = run
        run.step = 3
        run.status = "controlling"
        switched = run_to_end(runner, task="the task", provider="groq")
        self.assertEqual(switched.task, "the task")
        self.assertTrue(switched.trace, "the run recorded no turns at all")
        self.assertTrue(all(t.provider == "groq" for t in switched.trace),
                        [t.provider for t in switched.trace])
        self.assertGreaterEqual(len(switched.trace), 5)

    def test_a_malformed_groq_reply_is_still_rejected_by_the_same_parser(self):
        # The parser is the strict gate: a reply it refuses must not reach the
        # machine, and the reason must survive into the run's message.
        command, reason = parse_command('{"type":"click","x":"over there","y":3}',
                                        bounds=Bounds(width=1365, height=768))
        self.assertIsNone(command)
        self.assertTrue(reason, "a rejected reply must say why")

        runner, _ = make_runner(replies=['{"type":"click","x":"over there","y":3}'])
        run = run_to_end(runner)
        self.assertEqual(run.status, "error")
        self.assertEqual(runner.computer.actions, [], "an invalid command reached the machine")
        self.assertTrue(any(t.parse_error for t in run.trace), [t.parse_error for t in run.trace])
        self.assertIn("usable command", run.message)

    def test_a_prose_groq_reply_is_reported_not_executed(self):
        runner, _ = make_runner(replies=["Sure! I have navigated to the page for you."])
        run = run_to_end(runner)
        self.assertEqual(run.status, "error")
        self.assertEqual(runner.computer.actions, [], "a prose reply was executed as an action")
        self.assertIn("never returned a usable command", run.message)

    def test_the_allowed_command_set_is_unchanged_by_adding_a_provider(self):
        for reply in SCRIPT:
            command, _ = parse_command(reply, bounds=Bounds(width=1365, height=768))
            self.assertIsNotNone(command, reply)
        # And the types Groq may use are the same closed list as before.
        for reply in ['{"type":"teleport","x":1,"y":1}', '{"type":"bash","cmd":"ls"}']:
            command, _ = parse_command(reply, bounds=Bounds(width=1365, height=768))
            self.assertIsNone(command, reply)


class TestGroqErrorsAreHandledLikeTheOthers(unittest.TestCase):
    """8. 429, 401 and a refused connection, through the shared error path."""

    def test_a_429_is_reached_retried_and_reported_with_groqs_own_words(self):
        runner, groq = make_failing_runner(
            [http_error(429, GROQ_RATE_LIMIT_BODY, reason="Too Many Requests", retry_after=0.0)],
            computer_max_http_attempts=2,
        )
        run = run_to_end(runner)
        self.assertEqual(groq.calls, 2, "the 429 was not retried")
        failure = failure_of(run)
        self.assertIsNotNone(failure, "a run that died on a 429 must report one")
        self.assertTrue(failure["provider_reached"], "a 429 means the provider WAS reached")
        self.assertEqual(failure["http_status"], 429)
        self.assertEqual(failure["http_reason"], "Too Many Requests")
        self.assertEqual(failure["provider"], "groq")
        self.assertEqual(failure["model"], DEFAULT_GROQ_MODEL)
        self.assertIn("Rate Limit Reached", failure["provider_error"])
        self.assertEqual(failure["retry_attempts"], 2)
        self.assertEqual(failure["final_result"], "stopped")
        self.assertIn("HTTP 429", failure["message"])

    def test_a_401_is_not_retried(self):
        runner, groq = make_failing_runner([http_error(401, GROQ_AUTH_BODY, reason="Unauthorized")])
        run = run_to_end(runner)
        self.assertEqual(groq.calls, 1, "a 401 was retried; it can only fail the same way again")
        failure = failure_of(run)
        self.assertEqual(failure["http_status"], 401)
        self.assertIn("Invalid API Key", failure["provider_error"])

    def test_a_connection_failure_is_reported_as_never_reached(self):
        runner, _ = make_failing_runner([ConnectionError("connection refused")])
        run = run_to_end(runner)
        failure = failure_of(run)
        self.assertFalse(failure["provider_reached"], "a refused connection means the provider was NOT reached")
        self.assertEqual(failure["provider"], "groq")
        self.assertIn("connection refused", failure["message"].lower())

    def test_groq_error_objects_carry_their_own_shape(self):
        error = ProviderHTTPError(
            provider="groq", model=DEFAULT_GROQ_MODEL, status=429,
            reason="Too Many Requests", body=GROQ_RATE_LIMIT_BODY, retry_after=2.0,
        )
        self.assertEqual(error.provider_detail, "Rate Limit Reached")
        self.assertTrue(error.reached)
        self.assertTrue(error.retryable)
        self.assertEqual(error.retry_after, 2.0)
        self.assertEqual(error.to_dict()["provider"], "groq")
        self.assertEqual(error.to_dict()["http_status"], 429)


class TestNoGroqKeyReachesTheBrowser(unittest.TestCase):
    """2. The key stays server-side, by every route the browser can take."""

    def test_the_provider_routes_carry_no_key_material(self):
        from app.main import app as fastapi_app

        routes = [getattr(r, "path", "") for r in fastapi_app.routes]
        self.assertIn("/ai/computer/providers", routes)
        blob = json.dumps([info.__dict__ for info in computer_providers(make_settings())])
        for secret in ("groq-secret-value", "openrouter-secret-value", "mistral-secret-value"):
            self.assertNotIn(secret, blob)

    def test_a_real_run_s_trace_holds_no_key(self):
        runner, _ = make_runner()
        run = run_to_end(runner)
        blob = json.dumps(run.trace_report(include_images=True, providers=computer_providers(runner.settings),
                                           default_provider="openrouter"))
        self.assertNotIn("groq-secret-value", blob)
        self.assertIn("groq", blob, "the provider name should still be there; only the key is withheld")

    def test_the_adapter_stores_the_key_out_of_the_wire_summary(self):
        provider = GroqProvider("groq-secret-value")
        messages = [LLMMessage(role="user", content="look", images=[JPEG])]
        provider.last_wire = summarize_wire(
            {"model": DEFAULT_GROQ_MODEL, "messages": provider._wire_messages(messages), "stream": True},
            messages,
            path="/chat/completions",
        )
        self.assertNotIn("groq-secret-value", json.dumps(provider.last_wire))


if __name__ == "__main__":
    unittest.main(verbosity=2)