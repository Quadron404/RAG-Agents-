"""The second computer-control provider.

Mistral is a peer of OpenRouter, not a second implementation of computer
control.  Everything that decides what the machine does -- the prompt, the
history, the screenshot, the strict JSON parser, the executors, the trace --
lives above the provider interface and is deliberately not re-implemented
here.  What these tests pin down is the narrow claim that makes the two
interchangeable: swapping the provider changes *who answers* and nothing else.

So the tests below are mostly about sameness:

- the same commands parse, whichever provider produced them;
- the same complete conversation is sent to both;
- the same latest screenshot reaches both, as a real image part;
- switching mid-run keeps the task, the history and the step count;
- a missing key is reported by name, from both the start route and the loop.

And two about Mistral specifically: that the adapter is a real
OpenAI-compatible one rather than a copy that will drift, and that no key
reaches the browser by any route.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.computer.commands import ALLOWED_TYPES, Bounds, parse_command
from app.computer.prompt import build_prompt
from app.computer.runner import ComputerRun, ComputerRunner
from app.config import load_settings
from app.providers.base import LLMMessage, summarize_wire
from app.providers.mistral import DEFAULT_MISTRAL_MODEL, MistralProvider
from app.providers.openai_compat import OpenAICompatProvider
from app.providers.router import (
    ProviderUnavailable,
    Router,
    computer_model_for,
    computer_provider_info,
    computer_providers,
)
from tests.test_computer_control import SCREEN, FakeComputer, FakeProvider, _finish

#: The OpenRouter reply set every provider is asked to drive, so the two are
#: provably running the same loop rather than two loops that happen to work.
SCRIPT = [
    '{"type":"navigate","url":"https://example.com/computer-test.html"}',
    '{"type":"click","x":700,"y":350}',
    '{"type":"type","text":"example.com"}',
    '{"type":"key","key":"ENTER"}',
    '{"type":"done","message":"Example Domain"}',
]

JPEG = base64.b64encode(bytes.fromhex("ffd8ffd9")).decode("ascii")  # a real JPEG header


def make_settings(**overrides):
    """Settings with both providers configured, so selection is the only variable."""
    settings = load_settings()
    settings.computer_provider = "openrouter"
    settings.computer_model = "test/vision-openrouter"
    settings.openrouter_api_key = "sk-or-secret-value"
    settings.mistral_api_key = "mistral-secret-value"
    settings.mistral_model = DEFAULT_MISTRAL_MODEL
    settings.computer_max_steps = 8
    settings.computer_max_json_retries = 2
    settings.computer_settle_ms = 0
    settings.computer_settle_ms_click = 0
    settings.workspace_base_url = "http://127.0.0.1:9"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def make_two_provider_runner(replies=None, provider=None, **settings_overrides):
    """A runner whose Router holds both providers, each independently recorded."""
    settings = make_settings(**settings_overrides)
    openrouter = FakeProvider(list(replies or SCRIPT))
    openrouter.name = "openrouter"
    mistral = FakeProvider(list(replies or SCRIPT))
    mistral.name = "mistral"
    router = Router({"openrouter": openrouter, "mistral": mistral}, settings)
    runner = ComputerRunner(settings, router, db=None)
    runner.computer = FakeComputer(SCREEN)
    run = ComputerRun(task_id="t", task="the task", provider=provider or "")
    return runner, run, {"openrouter": openrouter, "mistral": mistral}


def provider_of(turn_report):
    return turn_report["provider"]


async def _finish_with(runner, task: str, provider: str = ""):
    """``_finish``, but starting the run pointed at a named provider.

    The shared helper calls ``runner.start(task)`` with no provider, which is
    right for every test that does not care and wrong for every test here:
    starting a run pointed at Mistral has to go through the same public
    ``start()`` the HTTP route uses, or these tests would be exercising a
    back door.
    """
    run = await runner.start(task, provider=provider)
    handle = runner._tasks[run.task_id]
    for _ in range(200):
        if handle.done():
            break
        await asyncio.sleep(0.01)
    if not handle.done():
        handle.cancel()
    return run


class TestMistralIsInitialisedFromItsOwnEnvironment(unittest.TestCase):
    """1. Mistral provider initialisation."""

    def test_the_model_defaults_to_the_documented_one(self):
        # A configured key with no model named is a missing variable, not a
        # request for a text-only model.  Falling back to a default that can
        # take images means the run works; failing means the loop dies on turn
        # one having been told there is no screenshot to look at.
        self.assertEqual(DEFAULT_MISTRAL_MODEL, "mistral-small-2506")
        settings = make_settings(mistral_model="")
        self.assertEqual(computer_model_for(settings, "mistral"), "mistral-small-2506")

    def test_an_explicit_model_wins_over_the_default(self):
        settings = make_settings(mistral_model="mistral-medium-2505")
        self.assertEqual(computer_model_for(settings, "mistral"), "mistral-medium-2505")

    def test_the_two_providers_never_share_a_model(self):
        # Sharing one would make "which model was this" ambiguous, and a run
        # that silently used OpenRouter's model against Mistral's key would
        # 404 rather than misbehave -- a worse, more confusing failure.
        settings = make_settings()
        self.assertNotEqual(
            computer_model_for(settings, "openrouter"),
            computer_model_for(settings, "mistral"),
        )

    def test_the_provider_is_registered_only_when_the_key_is_present(self):
        import app.providers as providers_module

        settings = make_settings(mistral_api_key="", openrouter_api_key="")
        built = providers_module.build_providers(settings)
        self.assertNotIn("mistral", built, "registered with no key")
        self.assertNotIn("openrouter", built)

        settings = make_settings()
        built = providers_module.build_providers(settings)
        self.assertIn("mistral", built)
        self.assertIn("openrouter", built)
        self.assertIsInstance(built["mistral"], MistralProvider)
        self.assertEqual(built["mistral"].name, "mistral")
        self.assertEqual(built["mistral"].base_url, "https://api.mistral.ai/v1")

    def test_the_default_base_url_is_mistral_not_openrouter(self):
        # A provider pointed at the wrong host fails with a 401 that reads like
        # a bad key, which sends people off to rotate a perfectly good secret.
        settings = make_settings()
        self.assertEqual(settings.mistral_base_url, "https://api.mistral.ai/v1")
        self.assertNotEqual(settings.mistral_base_url, settings.openrouter_base_url)


class TestTheMistralAdapterIsTheOpenAICompatibleOne(unittest.TestCase):
    """2, 3. The multimodal request, as text plus an image part."""

    def _wire(self, provider):
        return provider._wire_messages(
            [
                LLMMessage(role="system", content="the prompt"),
                LLMMessage(role="user", content="the newest state", images=[JPEG]),
            ]
        )

    def test_it_reuses_the_openai_compatible_serialiser_rather_than_copying_it(self):
        # If this ever stops being true, there are two implementations of "turn
        # a screenshot into an image part", and the one that ships is whichever
        # one is not under test here.
        self.assertTrue(issubclass(MistralProvider, OpenAICompatProvider))

    def test_the_screenshot_goes_on_as_a_real_image_part(self):
        wire = self._wire(MistralProvider("k"))
        parts = wire[1]["content"]
        self.assertIsInstance(parts, list, "the image turn is not a content-part list")
        kinds = [p["type"] for p in parts]
        self.assertIn("image_url", kinds)
        self.assertIn("text", kinds)
        url = next(p for p in parts if p["type"] == "image_url")["image_url"]["url"]
        # The actual bytes, inline.  A path, a description or a URL to fetch
        # would all produce a request that looks multimodal and is not.
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertIn(JPEG, url)

    def test_a_text_only_turn_stays_a_plain_string(self):
        wire = MistralProvider("k")._wire_messages([LLMMessage(role="user", content="just text")])
        self.assertEqual(wire[0]["content"], "just text")

    def test_the_image_carries_no_field_mistral_rejects(self):
        # `detail` is an OpenAI token-spend hint.  Mistral rejects it on an
        # image part, so it is stripped -- the one thing this adapter changes.
        wire = self._wire(MistralProvider("k"))
        for part in wire[1]["content"]:
            if part["type"] == "image_url":
                self.assertNotIn("detail", part["image_url"])

    def test_openrouter_keeps_the_detail_hint(self):
        # ...and OpenRouter does not lose it, which is what makes the override a
        # Mistral-only change rather than a change to the shared path.
        wire = OpenAICompatProvider("openrouter", "k", "https://openrouter.test/api/v1")._wire_messages(
            [LLMMessage(role="user", content="state", images=[JPEG])]
        )
        part = next(p for p in wire[0]["content"] if p["type"] == "image_url")
        self.assertEqual(part["image_url"]["detail"], "high")

    def test_the_wire_summary_describes_a_mistral_request_the_same_way(self):
        # The panel's "screenshot on the wire" chip reads the same summary for
        # both providers; a Mistral request that summarised differently would
        # make the trace untrustworthy for exactly the run you are debugging.
        messages = [LLMMessage(role="user", content="state", images=[JPEG])]
        mistral = MistralProvider("k")
        body = {"model": "m", "messages": mistral._wire_messages(messages), "stream": True}
        wire = summarize_wire(body, messages, path="/chat/completions")
        self.assertTrue(wire["image_present"])
        self.assertEqual(wire["image_mime"], "image/jpeg")
        self.assertEqual(wire["image_count"], 1)
        self.assertEqual(wire["content_part_types"], ["text", "image_url"])


class TestMistralRawRepliesGoThroughTheSameParser(unittest.TestCase):
    """4, 10. Raw response parsing and the command schema."""

    def test_a_mistral_reply_is_stored_raw_and_parsed_unchanged(self):
        # The navigate has to come first: a bare click on turn one is refused by
        # the parser because there is no screenshot yet, and it would be the
        # parser being right rather than Mistral being wrong.
        click = '{"type":"click","x":742,"y":418}'
        runner, run, _ = make_two_provider_runner(
            replies=['{"type":"navigate","url":"https://example.com"}', click],
            provider="mistral",
        )
        report = asyncio.run(_finish_with(runner, run.task, provider="mistral")).trace_report()
        turn = [t for t in report["turns"] if t["command"].get("type") == "click"][0]
        self.assertEqual(turn["raw"], click, "the reply was rewritten on the way to the trace")
        self.assertTrue(turn["parse_ok"])
        self.assertEqual(turn["command"], {"type": "click", "x": 742, "y": 418})
        self.assertEqual(provider_of(turn), "mistral")

    def test_the_parser_does_not_know_which_provider_was_asked(self):
        # parse_command takes no provider, which is the point.  Asserted rather
        # than assumed, because a provider argument threaded into the parser is
        # the first step towards two dialects.
        import inspect

        self.assertNotIn("provider", inspect.signature(parse_command).parameters)

    def test_every_allowed_command_parses_identically_whichever_provider_replied(self):
        for kind in ALLOWED_TYPES:
            payload = {
                "navigate": '{"type":"navigate","url":"https://google.com"}',
                "search": '{"type":"search","query":"x.com"}',
                "click": '{"type":"click","x":742,"y":418}',
                "type": '{"type":"type","text":"hello"}',
                "key": '{"type":"key","key":"ENTER"}',
                "scroll": '{"type":"scroll","delta_y":500}',
                "move": '{"type":"move","x":800,"y":300}',
                "done": '{"type":"done","message":"Task complete."}',
                "error": '{"type":"error","message":"stuck"}',
            }[kind]
            for provider in ("openrouter", "mistral"):
                with self.subTest(kind=kind, provider=provider):
                    cmd, err = parse_command(payload, bounds=SCREEN)
                    self.assertIsNotNone(cmd, f"{provider}: {err}")
                    self.assertEqual(cmd.type, kind)

    def test_a_mistral_run_of_the_whole_script_executes_every_step(self):
        # The full navigate -> screenshot -> click -> screenshot -> type ->
        # screenshot -> key -> screenshot -> done chain, driven by Mistral.
        runner, run, _ = make_two_provider_runner(provider="mistral")
        report = asyncio.run(_finish_with(runner, run.task, provider="mistral")).trace_report()
        self.assertEqual([t["command"].get("type") for t in report["turns"]],
                         ["navigate", "click", "type", "key", "done"])
        for turn in report["turns"]:
            self.assertEqual(turn["provider"], "mistral")
            self.assertEqual(turn["model"], DEFAULT_MISTRAL_MODEL)
        # A screenshot on every turn after the navigate, and a fresh one after
        # each non-terminal action.
        for turn in report["turns"][1:]:
            self.assertTrue(turn["screenshot_attached"], f"turn {turn['turn']} had no screenshot")
            self.assertTrue(turn["wire"]["image_present"], f"turn {turn['turn']} sent no image")
        for turn in report["turns"][:-1]:
            self.assertTrue(turn["next_image"], f"turn {turn['turn']} captured no after-shot")
        # The executors ran, not just the parser.  "Return" rather than "ENTER"
        # because the parser normalises key names for xdotool, above the provider
        # -- a model may answer with either and the machine gets the same key.
        self.assertEqual(
            [t["execution"]["outcome"] for t in report["turns"]],
            ["executed", "executed", "executed", "executed", "done"],
        )
        self.assertEqual(runner.computer.actions, [
            ("navigate", "https://example.com/computer-test.html"),
            ("click", 700.0, 350.0),
            ("type", "example.com"),
            ("key", "Return"),
        ])


class TestBothProvidersGetTheWholeConversationAndTheNewestFrame(unittest.TestCase):
    """8, 9, 6. The complete conversation and the latest screenshot, to both."""

    def _run_with(self, provider):
        runner, run, _ = make_two_provider_runner(provider=provider)
        report = asyncio.run(_finish_with(runner, "the task", provider=provider)).trace_report()
        return runner, report

    def test_the_two_providers_are_sent_an_identical_conversation(self):
        # Same roles, same order, same text, same image, same prompt.  Anything
        # less and the comparison of their behaviour is not a comparison.
        _, openrouter = self._run_with("openrouter")
        _, mistral = self._run_with("mistral")
        self.assertEqual(len(openrouter["turns"]), len(mistral["turns"]))
        for a, b in zip(openrouter["turns"], mistral["turns"]):
            self.assertEqual(a["turn"], b["turn"])
            self.assertEqual(a["prompt"], b["prompt"])
            self.assertEqual(
                [m["role"] for m in a["messages_meta"]],
                [m["role"] for m in b["messages_meta"]],
            )
            self.assertEqual(a["message_count"], b["message_count"])
            self.assertEqual(a["image"], b["image"])
            self.assertEqual(a["user_text"], b["user_text"])
            self.assertEqual(a["command"], b["command"])
            self.assertEqual(a["execution"]["outcome"], b["execution"]["outcome"])

    def test_the_history_only_grows_and_keeps_the_whole_conversation(self):
        for provider in ("openrouter", "mistral"):
            with self.subTest(provider=provider):
                runner, report = self._run_with(provider)
                counts = [t["message_count"] for t in report["turns"]]
                self.assertEqual(counts, sorted(counts))
                # system + task, then a state turn per completed action.
                self.assertEqual(counts[0], 2)
                self.assertEqual(counts[-1], 2 + 2 * (len(counts) - 1))

    def test_every_request_carries_the_prompt_and_the_screenshot_from_the_last_capture(self):
        for provider in ("openrouter", "mistral"):
            with self.subTest(provider=provider):
                runner, report = self._run_with(provider)
                expected = 0
                for turn in report["turns"]:
                    if turn["first_turn"]:
                        self.assertFalse(turn["screenshot_attached"], "the first turn had nothing to show yet")
                    else:
                        expected += 1
                        self.assertEqual(
                            turn["image"], f"SCREENSHOT-{expected}",
                            f"turn {turn['turn']} was sent a stale frame",
                        )
                        self.assertTrue(turn["wire"]["image_present"])

    def test_the_conversation_is_rebuilt_from_the_run_not_held_by_the_provider(self):
        # No provider-side conversation id anywhere.  A run pointed at a second
        # provider has to carry the same context, and the only way to guarantee
        # that is if the context was never the provider's to begin with.
        for provider in ("openrouter", "mistral"):
            with self.subTest(provider=provider):
                runner, _, _ = make_two_provider_runner(provider=provider)
                fake = runner.router.providers[provider]
                asyncio.run(_finish_with(runner, "the task", provider=provider))
                self.assertTrue(fake.calls, f"{provider} was never called")
                for call in fake.calls:
                    self.assertTrue(any(m.role == "system" for m in call))
                    self.assertTrue(any("the task" in (m.content or "") for m in call))
                    for m in call:
                        self.assertFalse(getattr(m, "conversation_id", None))


class TestSwitchingProviderMidRun(unittest.TestCase):
    """7. Switching provider."""

    def test_a_switch_changes_who_answers_without_touching_the_task(self):
        runner, run, _ = make_two_provider_runner(provider="openrouter")
        # Answer the first turn from OpenRouter, then switch before the second.
        # The loop records the turn before calling, so the second call is the one
        # that sees two records -- switching on `== 1` would fire before the
        # first request was ever made and this test would prove nothing.
        original = runner._ask

        async def switching(messages, run_arg):
            if len(run_arg.trace) >= 2:
                run_arg.provider = "mistral"
            return await original(messages, run_arg)

        runner._ask = switching
        report = asyncio.run(_finish_with(runner, run.task, provider="openrouter")).trace_report()
        names = [t["provider"] for t in report["turns"]]
        self.assertEqual(names[0], "openrouter", "the first reply was misattributed")
        self.assertIn("mistral", names, "the switch never took effect")
        # One clean boundary, in order: everything before it OpenRouter,
        # everything after it Mistral.
        self.assertEqual(names, ["openrouter"] * (names.count("openrouter")) + ["mistral"] * names.count("mistral"))
        self.assertEqual(report["task"], "the task")
        # The conversation did not restart: the message count kept growing
        # across the boundary rather than dropping back to two.
        counts = [t["message_count"] for t in report["turns"]]
        self.assertEqual(counts, sorted(counts))

    def test_the_history_survives_the_switch_in_full(self):
        runner, run, _ = make_two_provider_runner(provider="openrouter")
        original = runner._ask

        async def switching(messages, run_arg):
            if len(run_arg.trace) >= 2:
                run_arg.provider = "mistral"
            return await original(messages, run_arg)

        runner._ask = switching
        asyncio.run(_finish_with(runner, run.task, provider="openrouter"))
        mistral = runner.router.providers["mistral"]
        self.assertTrue(mistral.calls, "Mistral was never called")
        first_mistral_call = mistral.calls[0]
        # Everything OpenRouter had already been told, Mistral is told too.
        self.assertTrue(any(m.role == "system" for m in first_mistral_call))
        self.assertTrue(any(m.role == "assistant" for m in first_mistral_call), "no prior replies carried over")
        self.assertTrue(any("the task" in (m.content or "") for m in first_mistral_call))

    def test_set_provider_leaves_the_step_count_and_the_task_alone(self):
        runner, run, _ = make_two_provider_runner(provider="openrouter")
        runner._runs[run.task_id] = run
        run.step = 3
        runner.set_provider(run.task_id, "mistral")
        self.assertEqual(run.provider, "mistral")
        self.assertEqual(run.task, "the task")
        self.assertEqual(run.step, 3)

    def test_set_provider_on_an_unknown_run_is_refused(self):
        runner, run, _ = make_two_provider_runner(provider="openrouter")
        runner._runs.pop(run.task_id, None)
        with self.assertRaises(KeyError):
            runner.set_provider(run.task_id, "mistral")


class TestTheProviderSelectorIsAnApi(unittest.TestCase):
    """5. The selector, over the real ASGI stack."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        import app.main as main_module

        cls.main = main_module
        cls.client = TestClient(main_module.app)

    def setUp(self):
        self._saved = {
            "openrouter_api_key": self.main.settings.openrouter_api_key,
            "mistral_api_key": self.main.settings.mistral_api_key,
            "mistral_model": self.main.settings.mistral_model,
            "computer_model": self.main.settings.computer_model,
        }

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(self.main.settings, key, value)

    def test_it_lists_every_provider_with_its_model(self):
        self.main.settings.openrouter_api_key = "sk-or-secret"
        self.main.settings.mistral_api_key = "mistral-secret"
        self.main.settings.computer_model = "openrouter/vision"
        self.main.settings.mistral_model = DEFAULT_MISTRAL_MODEL
        # Groq is listed as a peer, unconfigured here, so this stays a check of
        # the two configured providers without hiding that Groq exists.
        self.main.settings.groq_api_key = ""
        body = self.client.get("/ai/computer/providers").json()
        self.assertEqual([p["name"] for p in body["providers"]], ["openrouter", "mistral", "groq"])
        by_name = {p["name"]: p for p in body["providers"]}
        self.assertEqual(by_name["openrouter"]["label"], "OpenRouter")
        self.assertEqual(by_name["openrouter"]["model"], "openrouter/vision")
        self.assertEqual(by_name["mistral"]["label"], "Mistral")
        self.assertEqual(by_name["mistral"]["model"], "mistral-small-2506")
        self.assertTrue(all(p["configured"] for p in body["providers"][:2]))
        self.assertFalse(by_name["groq"]["configured"], "Groq has no key here")

    def test_an_unconfigured_provider_is_listed_and_marked_not_hidden(self):
        # Hidden, it would be an invisible missing Codespaces secret until
        # somebody went looking.  Marked, the panel can say why it will fail.
        self.main.settings.openrouter_api_key = "sk-or-secret"
        self.main.settings.mistral_api_key = ""
        body = self.client.get("/ai/computer/providers").json()
        by_name = {p["name"]: p for p in body["providers"]}
        self.assertIn("mistral", by_name)
        self.assertFalse(by_name["mistral"]["configured"])
        self.assertTrue(by_name["openrouter"]["configured"])

    def test_starting_a_run_on_mistral_is_refused_by_name_when_it_has_no_key(self):
        self.main.settings.mistral_api_key = ""
        response = self.client.post("/ai/computer/start", json={"task": "go", "provider": "mistral"})
        self.assertEqual(response.json()["error"], "Mistral is not configured")

    def test_starting_a_run_on_openrouter_is_refused_by_name_when_it_has_no_key(self):
        self.main.settings.openrouter_api_key = ""
        response = self.client.post("/ai/computer/start", json={"task": "go", "provider": "openrouter"})
        self.assertEqual(response.json()["error"], "OpenRouter is not configured")

    def test_the_two_failures_are_distinguishable(self):
        # "computer control is not configured" with no subject is a puzzle.
        # The two providers are configured independently and either can be the
        # missing one, so the message has to say which.
        self.main.settings.openrouter_api_key = ""
        self.main.settings.mistral_api_key = ""
        first = self.client.post("/ai/computer/start", json={"task": "go", "provider": "openrouter"}).json()["error"]
        second = self.client.post("/ai/computer/start", json={"task": "go", "provider": "mistral"}).json()["error"]
        self.assertNotEqual(first, second)
        self.assertIn("OpenRouter", first)
        self.assertIn("Mistral", second)

    def test_an_unknown_provider_is_refused(self):
        response = self.client.post("/ai/computer/start", json={"task": "go", "provider": "skynet"})
        self.assertIn("unknown", response.json()["error"])

    def test_switching_provider_on_a_live_run_returns_the_model_it_will_use(self):
        self.main.settings.mistral_api_key = "mistral-secret"
        self.main.settings.mistral_model = DEFAULT_MISTRAL_MODEL
        run = ComputerRun(task_id="switch-me", task="go", provider="openrouter")
        self.main.ai_computer._runs[run.task_id] = run
        self.addCleanup(self.main.ai_computer._runs.pop, run.task_id, None)
        body = self.client.post(f"/ai/computer/{run.task_id}/provider", json={"provider": "mistral"}).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["provider"], "mistral")
        self.assertEqual(body["model"], DEFAULT_MISTRAL_MODEL)
        self.assertEqual(run.provider, "mistral")

    def test_switching_to_an_unconfigured_provider_is_refused_and_changes_nothing(self):
        self.main.settings.mistral_api_key = ""
        run = ComputerRun(task_id="switch-me-2", task="go", provider="openrouter")
        self.main.ai_computer._runs[run.task_id] = run
        self.addCleanup(self.main.ai_computer._runs.pop, run.task_id, None)
        response = self.client.post(f"/ai/computer/{run.task_id}/provider", json={"provider": "mistral"})
        self.assertEqual(response.json()["error"], "Mistral is not configured")
        self.assertEqual(run.provider, "openrouter", "a refused switch still changed the run")

    def test_switching_on_an_unknown_task_is_refused(self):
        response = self.client.post("/ai/computer/no-such-task/provider", json={"provider": "mistral"})
        self.assertEqual(response.json()["error"], "no such computer-control task")

    def test_the_trace_reports_the_selection_and_the_provider_that_answered(self):
        self.main.settings.mistral_api_key = "mistral-secret"
        runner, run, _ = make_two_provider_runner(provider="mistral")
        run = asyncio.run(_finish_with(runner, "go", provider="mistral"))
        self.main.ai_computer._runs[run.task_id] = run
        self.addCleanup(self.main.ai_computer._runs.pop, run.task_id, None)
        body = self.client.get(f"/ai/computer/{run.task_id}/trace?images=false").json()
        self.assertEqual(body["provider"], "mistral")
        self.assertEqual(body["last_provider"], "mistral")
        self.assertEqual(body["last_model"], DEFAULT_MISTRAL_MODEL)
        self.assertIn("mistral", [p["name"] for p in body["selected_providers"]])


class TestTheRouterNamesTheProviderThatFailed(unittest.TestCase):
    """11, 12. Missing-key behaviour, from the loop as well as the route."""

    def test_a_mistral_run_with_no_mistral_provider_says_mistral(self):
        settings = make_settings()
        router = Router({"openrouter": FakeProvider(SCRIPT)}, settings)
        with self.assertRaises(ProviderUnavailable) as caught:
            router.resolve("computer", provider_name="mistral")
        self.assertEqual(str(caught.exception), "Mistral is not configured")

    def test_an_openrouter_run_with_no_openrouter_provider_says_openrouter(self):
        settings = make_settings()
        router = Router({"mistral": FakeProvider(SCRIPT)}, settings)
        with self.assertRaises(ProviderUnavailable) as caught:
            router.resolve("computer", provider_name="openrouter")
        self.assertEqual(str(caught.exception), "OpenRouter is not configured")

    def test_the_loop_reports_the_missing_provider_and_stops(self):
        # The run must not crash, and the status must say which provider.
        settings = make_settings()
        router = Router({"openrouter": FakeProvider(SCRIPT)}, settings)
        runner = ComputerRunner(settings, router, db=None)
        runner.computer = FakeComputer(SCREEN)
        report = asyncio.run(_finish_with(runner, "go", provider="mistral")).trace_report()
        run = runner._runs[next(iter(runner._runs))]
        self.assertEqual(run.status, "error")
        self.assertIn("Mistral is not configured", run.message)
        # And the trace attributes it, so the panel does not show an
        # unattributed error next to a selector reading "Mistral".
        self.assertEqual(report["turns"][0]["provider"], "mistral")
        self.assertIn("Mistral is not configured", report["turns"][0]["error"])
        self.assertEqual(report["last_provider"], "mistral")

    def test_a_provider_with_a_key_but_no_model_is_refused_by_name(self):
        settings = make_settings(computer_model="")
        router = Router({"openrouter": FakeProvider(SCRIPT)}, settings)
        with self.assertRaises(ProviderUnavailable) as caught:
            router.resolve("computer", provider_name="openrouter")
        self.assertIn("OpenRouter is not configured", str(caught.exception))

    def test_the_computer_role_still_refuses_to_fall_back_to_the_mock(self):
        # A mock would answer a screenshot-driven task with invented commands
        # and the loop would "work" while touching nothing.
        settings = make_settings()
        router = Router({"mock": object()}, settings)
        with self.assertRaises(ProviderUnavailable):
            router.resolve("computer", provider_name="mistral")


class TestKeysNeverReachTheBrowser(unittest.TestCase):
    """13. No key in any response, any route, any bundle."""

    SECRETS = ("sk-or-secret-value", "mistral-secret-value", "sk-or-secret", "mistral-secret")

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        import app.main as main_module

        cls.main = main_module
        cls.client = TestClient(main_module.app)

    def _assert_clean(self, blob, where):
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob, f"{where} leaked a key")

    def test_the_provider_list_carries_no_key(self):
        self.main.settings.openrouter_api_key = "sk-or-secret"
        self.main.settings.mistral_api_key = "mistral-secret"
        response = self.client.get("/ai/computer/providers")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("api_key", response.text)
        self.assertNotIn("key", json.loads(response.text)["providers"][0])
        self._assert_clean(response.text, "/ai/computer/providers")

    def test_the_trace_carries_no_key_for_either_provider(self):
        for provider in ("openrouter", "mistral"):
            with self.subTest(provider=provider):
                self.main.settings.mistral_api_key = "mistral-secret"
                self.main.settings.openrouter_api_key = "sk-or-secret"
                runner, run, _ = make_two_provider_runner(provider=provider)
                asyncio.run(_finish_with(runner, "go", provider=provider))
                self.main.ai_computer._runs[run.task_id] = run
                self.addCleanup(self.main.ai_computer._runs.pop, run.task_id, None)
                response = self.client.get(f"/ai/computer/{run.task_id}/trace")
                self._assert_clean(response.text, f"the {provider} trace")
                self.assertNotIn("api_key", response.text)
                self.assertNotIn("Authorization", response.text)

    def test_the_wire_summary_never_carries_the_key_or_a_header(self):
        # summarize_wire reads the body, and the body has no key in it -- the
        # key is only ever in a header.  Asserted so that a future change which
        # merged them would be caught here rather than in a shared screenshot.
        for cls_, key in ((MistralProvider, "mistral-secret"), (OpenAICompatProvider, "sk-or-secret")):
            with self.subTest(provider=cls_.__name__):
                if cls_ is OpenAICompatProvider:
                    provider = cls_("openrouter", key, "https://openrouter.test/api/v1")
                else:
                    provider = cls_(key)
                messages = [LLMMessage(role="user", content="hi", images=[JPEG])]
                body = {"model": "m", "messages": provider._wire_messages(messages)}
                provider.last_wire = summarize_wire(body, messages)
                blob = json.dumps(provider.last_wire)
                self.assertNotIn(key, blob)
                self.assertNotIn("Authorization", blob)
                self.assertNotIn("Bearer", blob)
                # And still no base64, which is the point of the summary.
                self.assertNotIn(JPEG, blob)

    def test_no_frontend_file_mentions_a_key_variable(self):
        # The bundle is built from these sources; a VITE_ variable would be
        # inlined into the JavaScript and visible to anyone who opens it.
        root = Path(__file__).resolve().parents[2] / "Frontend" / "src"
        offenders = []
        for path in root.rglob("*"):
            if path.suffix not in (".ts", ".tsx", ".js", ".jsx", ".html", ".css"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in ("MISTRAL_API_KEY", "OPENROUTER_API_KEY", "VITE_MISTRAL", "VITE_OPENROUTER"):
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [], "a key variable reached the frontend source")

    def test_the_error_for_a_missing_key_does_not_echo_the_key(self):
        self.main.settings.mistral_api_key = ""
        self.main.settings.openrouter_api_key = ""
        response = self.client.post("/ai/computer/start", json={"task": "go", "provider": "mistral"})
        self._assert_clean(response.text, "the start route")


if __name__ == "__main__":
    unittest.main()
