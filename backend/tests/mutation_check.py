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
