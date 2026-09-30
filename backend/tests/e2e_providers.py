"""End-to-end: the exact task, both providers, through the real HTTP route.

This drives the real FastAPI app, the real router, the real prompt builder, the
real OpenAI-compatible serialiser, the real strict parser, the real executors
and the real trace, for both selectable providers, in one run each.

The only two things stubbed are the two that cannot be exercised from a
development machine: the outbound model call and the outbound X11/Chrome
action.  Everything between them is the code that ships.  It is therefore a
test of wiring and of the provider swap, and it is NOT evidence that Mistral
complies with the protocol or that a click lands on a real display -- those
need a live key and a live Chrome, and are reported separately.

Task under test, verbatim as specified:

    "Open google.com, search for example.com, click the result, and tell me
     the page title."
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app.computer.runner import ComputerRun, ComputerRunner
from app.config import load_settings
from app.providers.mistral import DEFAULT_MISTRAL_MODEL
from app.providers.router import Router
from tests.test_computer_control import SCREEN, FakeComputer, FakeProvider

TASK = "Open google.com, search for example.com, click the result, and tell me the page title."

# What a compliant model replies, one command at a time.  Note the screenshot
# goes on after the search, before the click -- the search is not the answer, and
# a loop that treats it as one never gets to a page.
REPLIES = [
    '{"type":"navigate","url":"https://google.com"}',
    '{"type":"search","query":"example.com"}',
    '{"type":"click","x":742,"y":418}',
    '{"type":"type","text":"Example Domain"}',
    '{"type":"key","key":"ENTER"}',
    '{"type":"done","message":"Example Domain"}',
]

EXPECTED_CHAIN = ["navigate", "search", "click", "type", "key", "done"]


def build(provider: str):
    # The routes read the app's own settings object, so that is what has to be
    # configured -- patching a private copy would make the "not configured"
    # refusal fire, which is exactly the check working rather than a nuisance.
    settings = main_module.settings
    settings.computer_provider = provider
    settings.computer_model = "openrouter/vision-test"
    settings.openrouter_api_key = "sk-or-not-a-real-key"
    settings.mistral_api_key = "mistral-not-a-real-key"
    settings.mistral_model = DEFAULT_MISTRAL_MODEL
    settings.workspace_base_url = "http://127.0.0.1:9"

    fake = FakeProvider(list(REPLIES))
    fake.name = provider
    router = Router({"openrouter": fake, "mistral": fake}, settings)
    runner = ComputerRunner(settings, router, db=None)
    runner.computer = FakeComputer(SCREEN)

    # Registered on the app's own runner so the real routes serve it, then the
    # task is dispatched through the real background task machinery.
    main_module.ai_computer.settings = settings
    main_module.ai_computer.router = router
    main_module.ai_computer.computer = FakeComputer(SCREEN)
    # Isolated per provider so the second run's trace cannot read the first's.
    main_module.ai_computer._runs = {}
    main_module.ai_computer._tasks = {}
    return settings, fake, runner


def main() -> int:
    client = TestClient(main_module.app)
    # The remote-computer check is real; stubbed here because there is no
    # Codespace to reach, and the alternative is a 4xx before the loop starts.
    main_module.computer.is_up = _async_true

    ok = True
    transcripts: dict[str, list[dict]] = {}

    for provider in ("openrouter", "mistral"):
        settings, fake, runner = build(provider)
        body = client.post("/ai/computer/start", json={"task": TASK, "provider": provider}).json()
        if "error" in body:
            print(f"FAIL {provider}: the start route refused the task: {body['error']}")
            ok = False
            continue
        task_id = body["task_id"]
        _wait(runner, task_id)

        report = client.get(f"/ai/computer/{task_id}/trace?images=false").json()
        turns = report["turns"]
        transcripts[provider] = turns

        print(f"\n{'=' * 72}\n{provider.upper()}  ({report['last_provider']} / {report['last_model']})"
              f"\n{'=' * 72}")
        print(f"task:    {report['task']}")
        print(f"status:  {report['status']} - {report['message']}")
        print(f"turns:   {len(turns)}")
        for t in turns:
            print(
                f"  #{t['turn']} {t['provider']:<10} msgs={t['message_count']:<2} "
                f"shot={'yes' if t['screenshot_attached'] else 'no ':<3} "
                f"wire_img={'yes' if t['wire']['image_present'] else 'no':<3} "
                f"-> {t['raw']}"
            )
        # Read the actions off the runner that actually executed the run, not the
        # local one -- otherwise this prints an empty list and looks like the
        # executors never ran.
        print(f"  actions on the machine: {main_module.ai_computer.computer.actions}")

        # The chain the task was supposed to produce.
        chain = [t["command"].get("type") for t in turns]
        if chain != EXPECTED_CHAIN:
            print(f"FAIL {provider}: the command chain was {chain}, expected {EXPECTED_CHAIN}")
            ok = False
        for t in turns:
            if t["provider"] != provider:
                print(f"FAIL {provider}: turn {t['turn']} was attributed to {t['provider']}")
                ok = False
            if not t["prompt_attached"]:
                print(f"FAIL {provider}: turn {t['turn']} had no computer-control prompt")
                ok = False
            if not t["parse_ok"]:
                print(f"FAIL {provider}: turn {t['turn']} did not parse: {t['parse_error']}")
                ok = False
        # Screenshot on every turn after the first, and a fresh one after each
        # non-terminal action.
        for t in turns[1:]:
            if not (t["screenshot_attached"] and t["wire"]["image_present"]):
                print(f"FAIL {provider}: turn {t['turn']} was sent without the screenshot")
                ok = False
        for t in turns[:-1]:
            # `images=false` removes the key entirely and flags it instead, so
            # the presence of a captured frame is asserted via the flag.
            if not (t.get("next_image") or t.get("image_withheld")):
                print(f"FAIL {provider}: turn {t['turn']} captured no after-shot")
                ok = False
        if turns[-1]["execution"]["outcome"] != "done":
            print(f"FAIL {provider}: the run ended as {turns[-1]['execution']['outcome']}, not done")
            ok = False

    # The two transcripts must be the same conversation, only differently
    # labelled.  This is the claim that matters: one loop, two providers.
    a, b = transcripts.get("openrouter"), transcripts.get("mistral")
    if a and b:
        print(f"\n{'=' * 72}\nARE THEY THE SAME LOOP?\n{'=' * 72}")
        same = all(
            x["prompt"] == y["prompt"]
            and x["user_text"] == y["user_text"]
            and x.get("image") == y.get("image")
            and x.get("image_meta") == y.get("image_meta")
            and x["raw"] == y["raw"]
            and x["command"] == y["command"]
            and x["execution"]["outcome"] == y["execution"]["outcome"]
            and x["message_count"] == y["message_count"]
            and x["wire"]["image_present"] == y["wire"]["image_present"]
            for x, y in zip(a, b)
        )
        print(f"identical apart from the provider label: {same}")
        if not same:
            ok = False

    # No key anywhere in either payload.
    print(f"\n{'=' * 72}\nSECRETS\n{'=' * 72}")
    blob = json.dumps(transcripts)
    leaked = [s for s in ("sk-or-not-a-real-key", "mistral-not-a-real-key") if s in blob]
    print(f"keys present in the trace the panel renders: {leaked or 'none'}")
    if leaked:
        ok = False

    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


async def _async_true() -> bool:
    return True


def _wait(runner: ComputerRunner, task_id: str) -> None:
    async def drain():
        handle = runner._tasks.get(task_id)
        if handle is None:
            return
        for _ in range(400):
            if handle.done():
                break
            await asyncio.sleep(0.01)
        if not handle.done():
            handle.cancel()

    asyncio.run(drain())


if __name__ == "__main__":
    raise SystemExit(main())
