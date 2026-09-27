import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["COMMANDER_PROVIDER"] = "mock"
os.environ["WORKER_PROVIDER"] = "mock"
os.environ["BROWSER_PROVIDER"] = "mock"
os.environ["DATA_DIR"] = "./data-test"

from app.config import Settings, load_settings
from app.db import Database
from app.providers import build_providers
from app.providers.router import Router
from app.tools.executor import Executor
from app.agents.commander import Commander

settings = load_settings()
db = Database(os.path.join(settings.data_dir, "rag.db"))
providers = build_providers(settings)
router = Router(providers, settings)
executor = Executor(settings)
commander = Commander(router, executor)

events = []


def emit(event):
    events.append(event)
    print("EVENT:", json.dumps(event, ensure_ascii=False)[:200])


async def main():
    tid = db.create_thread("smoke-user", "smoke test")
    db.add_message(tid, "user", "search for the latest news about RAG", agent="user")
    result = await commander.run(
        "search for the latest news about RAG",
        [],
        emit,
    )
    print("\nFINAL TEXT:", result["text"][:400])
    print("\nREPORTS:", len(result["reports"]))
    print("\nEVENTS COUNT:", len(events))
    assert len(events) > 0
    assert result["text"], "commander should produce final text"
    assert result["reports"], "should produce at least one worker report"
    print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())