"""The /health report must probe the agent, not repeat a cached flag.

``WorkspaceManager._agent_up`` is only ever written by ``is_up()``, and
``describe()`` just reads it back.  /health used to call only ``describe()``, so
after a backend restart that raced the agent coming up it reported
``agent_up=false`` for the whole session -- and, symmetrically, it would keep
saying true after the agent had died.  These checks prove the route now asks the
agent on every request instead of reporting the last answer forever.

Run with:  python -m tests.test_health_probe
"""
from __future__ import annotations

from pathlib import Path

import tests.support as support  # noqa: F401  (configures DATA_DIR/secrets first)

import app.main as main_module

ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = ROOT / "backend" / "app" / "main.py"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(("ok    " if ok else "FAIL  ") + label)
    if not ok:
        failures.append(label)
    return ok


def strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


# --- static: the route probes before it reports ------------------------------
src = strip_comments(MAIN_PY.read_text(encoding="utf-8"))
health_start = src.index("async def health(")
health_end = src.index("@app.", health_start + 1)
health_body = src[health_start:health_end]

check("computer.is_up()" in health_body, "/health calls computer.is_up()")
check(
    "computer.describe()" in health_body,
    "/health still reports the computer's state",
)
check(
    health_body.index("computer.is_up()") < health_body.index("computer.describe()"),
    "/health probes the agent before reading its state",
)


# --- behavioural: a stale True is corrected on the next request ---------------
from fastapi.testclient import TestClient  # noqa: E402

calls: list[int] = []


async def _fake_is_up() -> bool:
    calls.append(1)
    main_module.computer._agent_up = False
    return False


def run_behavioural() -> None:
    original = main_module.computer.is_up
    main_module.computer.is_up = _fake_is_up  # type: ignore[assignment]
    try:
        # A stale success from an earlier probe, with the agent now gone.
        main_module.computer._agent_up = True
        client = TestClient(main_module.app)
        resp = client.get("/health")
        data = resp.json()
        check(resp.status_code == 200, "/health answers 200")
        check(bool(calls), "/health actually probed the agent")
        check(
            data.get("computer", {}).get("agent_up") is False,
            "a stale agent_up=true is corrected to the live answer",
        )
    finally:
        main_module.computer.is_up = original  # type: ignore[assignment]
        main_module.computer._agent_up = False


run_behavioural()

if failures:
    print(f"\n{len(failures)} failed")
    raise SystemExit(1)
print("\nPASS: /health reports the live agent state, not a cached one")
