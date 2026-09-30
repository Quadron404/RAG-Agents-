"""Break the inspector on purpose and confirm a test notices.

Each mutation removes exactly one guarantee the inspector claims. If the suite
stays green, that guarantee is not actually being tested, and the inspector can
lie without anyone finding out until it does in a real run.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CASES = [
    (
        "wire says the image was never sent",
        "backend/app/providers/base.py",
        '"image_present": image_count > 0,',
        '"image_present": False,',
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "wire hides the image MIME",
        "backend/app/providers/base.py",
        '"image_mime": image_mimes[0] if image_mimes else "",',
        '"image_mime": "",',
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "trace records a different frame than the one sent",
        "backend/app/computer/runner.py",
        'turn.image = image or ""',
        'turn.image = "SOMETHING-ELSE"',
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "prompt is not attached",
        "backend/app/computer/runner.py",
        "turn.prompt_attached = system is not None",
        "turn.prompt_attached = True",
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "the prompt shown is the user's text rather than the system prompt",
        "backend/app/computer/runner.py",
        "turn.prompt = system.content if system else \"\"",
        "turn.prompt = messages[0].content if messages else \"\"",
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "raw reply is replaced by a friendly message",
        "backend/app/computer/runner.py",
        "turn.raw = raw",
        'turn.raw = "the model did not return a usable command"',
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "a refused action is reported as executed",
        "backend/app/computer/runner.py",
        '"executed": acted,',
        '"executed": True, "outcome": "executed",',
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "a deliberate done is reported as a failure",
        "backend/app/computer/runner.py",
        'if command.type == "done" and terminal:\n                        outcome = "done"',
        'if False:\n                        outcome = "done"',
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "retries collapse into one entry again",
        "backend/app/computer/runner.py",
        "turn=len(run.trace) + 1,",
        "turn=1,",
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "json_only is claimed without the strict parser",
        "backend/app/computer/runner.py",
        "json_only=True,",
        "json_only=False,",
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "the after-shot is the before-shot",
        "backend/app/computer/runner.py",
        "turn.next_image = image",
        "turn.next_image = turn.image",
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "the screenshot is dropped from the request",
        "backend/app/providers/openai_compat.py",
        'if m.images:',
        'if False:',
        "tests.test_computer_control.TestTheTraceAnswersTheFourQuestions",
    ),
    (
        "the prompt stops forbidding a search for the target text",
        "backend/app/computer/prompt.py",
        "Do NOT search for the text describing the target.",
        "You may search for the target text.",
        "tests.test_computer_control.TestThePromptTellsTheModelToActOnTheScreenshot",
    ),
    (
        "the trace route stops stripping secrets",
        "backend/app/main.py",
        "for turn in report[\"turns\"]:",
        "for turn in []:",
        "tests.test_computer_control.TestTheTraceEndpoint",
    ),
    (
        "a stripped screenshot is not marked as withheld",
        "backend/app/main.py",
        'turn["image_withheld"] = bool(turn.get("image") or turn.get("next_image"))',
        'turn["image_withheld"] = False',
        "tests.test_computer_control.TestTheTraceEndpoint",
    ),
    (
        "the wire summary claims the request was plain text",
        "backend/app/providers/base.py",
        '"text_parts": text_count,',
        '"text_parts": 0,',
        "tests.test_computer_control.TestTheWireSummaryDescribesTheRequestWithoutCopyingIt",
    ),
    # --- the second computer-control provider ------------------------------
    # A provider is only a provider if changing it changes nothing but the
    # name.  Each of these breaks one of the guarantees that makes that true,
    # and each is the kind of quiet divergence that turns "we support two
    # providers" into two subtly different control loops.
    (
        "mistral is sent a screenshot-less request",
        "backend/app/providers/mistral.py",
        "    def _wire_messages(self, messages: List[LLMMessage]) -> list:\n        wire = super()._wire_messages(messages)",
        "    def _wire_messages(self, messages: List[LLMMessage]) -> list:\n        wire = [m for m in super()._wire_messages(messages) if not isinstance(m.get('content'), list)]",
        "tests.test_computer_providers.TestTheMistralAdapterIsTheOpenAICompatibleOne",
    ),
    (
        "mistral stops being the openai-compatible serialiser",
        "backend/app/providers/mistral.py",
        "class MistralProvider(OpenAICompatProvider):",
        "class MistralProvider:",
        "tests.test_computer_providers.TestTheMistralAdapterIsTheOpenAICompatibleOne",
    ),
    (
        "mistral falls back to a text-only model with no name configured",
        "backend/app/providers/router.py",
        'return settings.mistral_model or "mistral-small-2506"',
        'return settings.mistral_model or "mistral-tiny"',
        "tests.test_computer_providers.TestMistralIsInitialisedFromItsOwnEnvironment",
    ),
    (
        "both providers are given the same model name",
        "backend/app/providers/router.py",
        'if name == "mistral":\n        return settings.mistral_model or "mistral-small-2506"\n    return settings.computer_model or ""',
        'return settings.computer_model or ""',
        "tests.test_computer_providers.TestMistralIsInitialisedFromItsOwnEnvironment",
    ),
    (
        "a missing key stops naming the provider that failed",
        "backend/app/providers/router.py",
        'raise ProviderUnavailable(f"{info.label} is not configured")',
        'raise ProviderUnavailable("computer control is not configured")',
        "tests.test_computer_providers.TestTheRouterNamesTheProviderThatFailed",
    ),
    (
        "the computer role falls back to the mock provider again",
        "backend/app/providers/router.py",
        'if role == "computer":\n            info = computer_provider_info(self.settings, chosen)',
        'if role == "computer" and provider is not None:\n            info = computer_provider_info(self.settings, chosen)',
        "tests.test_computer_providers.TestTheRouterNamesTheProviderThatFailed",
    ),
    (
        "the start route stops checking which provider was chosen",
        "backend/app/main.py",
        'if provider not in {i.name for i in computer_providers(settings)}:\n        return {"error": f"unknown computer provider {provider!r}"}',
        'if False:\n        return {"error": "unreachable"}',
        "tests.test_computer_providers.TestTheProviderSelectorIsAnApi",
    ),
    (
        "the start route stops refusing an unconfigured provider",
        "backend/app/main.py",
        '    info = computer_provider_info(settings, provider)\n    if not info.configured:',
        '    info = computer_provider_info(settings, provider)\n    if False:',
        "tests.test_computer_providers.TestTheProviderSelectorIsAnApi",
    ),
    (
        "a refused switch still changes the run",
        "backend/app/main.py",
        '    if not info.configured:\n        return {"error": f"{info.label} is not configured"}\n    if not computer_model_for(settings, provider):\n        return {"error": f"{info.label} is not configured: no model configured"}\n    try:',
        '    try:',
        "tests.test_computer_providers.TestTheProviderSelectorIsAnApi",
    ),
    (
        "the selector stops reporting which providers are configured",
        "backend/app/main.py",
        '{"name": i.name, "label": i.label, "model": i.model, "configured": i.configured}\n            for i in computer_providers(settings)',
        '{"name": i.name, "label": i.label, "model": i.model, "configured": True}\n            for i in computer_providers(settings)',
        "tests.test_computer_providers.TestTheProviderSelectorIsAnApi",
    ),
    (
        "the trace stops reporting who answered last",
        "backend/app/computer/runner.py",
        '"last_provider": self.trace[-1].provider if self.trace else "",',
        '"last_provider": "",',
        "tests.test_computer_providers.TestTheProviderSelectorIsAnApi",
    ),
    (
        "a provider switch resets the run instead of continuing it",
        "backend/app/computer/runner.py",
        "        run.provider = provider\n        return run",
        "        run.provider = provider\n        run.step = 0\n        return run",
        "tests.test_computer_providers.TestSwitchingProviderMidRun",
    ),
    (
        "the provider choice is ignored for every request",
        "backend/app/computer/runner.py",
        'provider, model = self.router.resolve("computer", provider_name=run.provider or None)',
        'provider, model = self.router.resolve("computer")',
        "tests.test_computer_providers.TestSwitchingProviderMidRun",
    ),
    (
        "the wire summary starts carrying the api key",
        "backend/app/providers/base.py",
        '"model": body.get("model"),',
        '"model": body.get("model"), "api_key": os.environ.get("OPENROUTER_API_KEY", "leaked"),',
        "tests.test_computer_providers.TestKeysNeverReachTheBrowser",
    ),
]


def run(tests: str) -> tuple:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", tests],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    survivors = []
    for name, rel, old, new, tests in CASES:
        path = ROOT / rel.removeprefix("backend/")
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP  {name}: pattern not found in {rel}")
            survivors.append(name)
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, out = run(tests)
        finally:
            path.write_text(original, encoding="utf-8")
        if code == 0:
            print(f"SURVIVED  {name}")
            survivors.append(name)
        else:
            first = next(
                (l for l in out.splitlines() if l.startswith(("FAIL:", "ERROR:"))),
                "failed",
            )
            print(f"caught    {name}  <- {first.strip()}")

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived -- these are untested guarantees:")
        for name in survivors:
            print(f"  - {name}")
        return 1
    print(f"all {len(CASES)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
