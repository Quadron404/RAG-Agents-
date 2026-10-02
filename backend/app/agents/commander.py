from __future__ import annotations

import json
import re
from typing import Callable, List, Optional

from ..providers.base import LLMMessage, TextDelta, stream_model
from ..providers.router import Router
from ..tools.executor import Executor
from ..tools.registry import commander_tools, worker_tools
from .loop import run_agent_loop

EmitFn = Callable[[dict], object]


PLAN_PROMPT = (
    "You are Commander, the coordinating brain of a multi-agent workforce. "
    "The user has an objective. Break it into 1-4 concrete subtasks that specialist "
    "worker agents can execute in parallel where possible. Return ONLY valid JSON with "
    "this exact shape: {\"objective\": string, \"steps\": [{\"title\": string, \"task\": string, "
    "\"compute\": \"browser\" | \"code\" | \"research\"}]}. Do not include markdown fences."
)

WORKER_PROMPT = (
    "You are a specialist worker agent inside a multi-agent system. You do NOT control "
    "the computer directly. You only perceive two things: text (task and tool outputs) "
    "and a screenshot image of the screen. You only produce text output.\n\n"
    "There is one shared computer (a microVM) with these tools: shell, files "
    "(read_file/write_file/list_dir), web_search, and a persistent browser.\n\n"
    "To tell the system to act on your behalf, end a sentence with an inline text tool "
    "call exactly like this:\n"
    "  [TOOL browser {\"action\":\"goto\",\"url\":\"https://example.com\"}]\n"
    "  [TOOL browser {\"action\":\"click\",\"x\":640,\"y\":400}]\n"
    "  [TOOL browser {\"action\":\"type\",\"text\":\"hello\",\"enter\":false}]\n"
    "  [TOOL browser {\"action\":\"text\"}]\n"
    "  [TOOL shell {\"command\":\"ls -la\"}]\n"
    "  [TOOL web_search {\"query\":\"latest linux kernel\"}]\n"
    "The system then executes it and returns the result (for browser actions a fresh "
    "screenshot is also returned, which you should look at carefully with your vision). "
    "The browser keeps one session across calls, so state persists; look at the "
    "screenshot before choosing click coordinates. Click x/y are pixel coordinates into "
    "a 1280x800 viewport. Do not fake results — only claim you did something after the "
    "system confirms it. Execute the assigned task to completion, then report your "
    "results as a concise factual summary (facts, numbers, code, or file paths), with "
    "no remaining [TOOL ...] markers."
)

SYNTH_PROMPT = (
    "You are Commander. Below are the reports from your worker agents for the user's "
    "objective. Integrate them into a single coherent final answer for the user. Be "
    "complete, cite what each worker did, and note anything that failed."
)


class Commander:
    def __init__(self, router: Router, executor: Executor):
        self.router = router
        self.executor = executor

    async def run(
        self,
        user_text: str,
        context: List[LLMMessage],
        emit: EmitFn,
        max_iters: int = 4,
    ) -> dict:
        plan = await self._make_plan(user_text, emit)
        await _emit_safe(emit, {"type": "plan", "plan": plan})

        reports = []
        steps = plan.get("steps") or []
        for i, step in enumerate(steps):
            title = step.get("title") or f"Step {i + 1}"
            task = step.get("task") or user_text
            compute = str(step.get("compute", "research")).lower()
            role = "browser" if compute == "browser" else "worker"
            agent_name = f"worker-{i + 1}"
            await _emit_safe(emit, {"type": "worker_start", "agent": agent_name, "title": title, "role": role})

            provider, model = self.router.resolve(role)
            result = await run_agent_loop(
                provider=provider,
                model=model,
                system=WORKER_PROMPT,
                context=context,
                user_text=task,
                tools=worker_tools(),
                executor=self.executor,
                emit=emit,
                agent_name=agent_name,
                max_iters=max_iters,
            )
            reports.append({"title": title, "role": role, "report": result["text"] or "(no output)"})

        await _emit_safe(emit, {"type": "synthesize"})
        provider_c, model_c = self.router.resolve("commander")
        synth_messages = [
            LLMMessage("user", self._render_reports(user_text, reports)),
        ]
        final_text = ""
        async for ev in stream_model(provider_c,
            [LLMMessage("system", SYNTH_PROMPT)] + synth_messages,
            [t.schema() for t in commander_tools()],
            model_c,
        ):
            if isinstance(ev, TextDelta):
                final_text += ev.content
                await _emit_safe(emit, {"type": "delta", "agent": "commander", "text": ev.content})

        return {"objective": user_text, "plan": plan, "reports": reports, "text": final_text.strip()}

    async def _make_plan(self, user_text: str, emit: EmitFn) -> dict:
        provider, model = self.router.resolve("commander")
        try:
            raw = ""
            async for ev in stream_model(provider,
                [LLMMessage("system", PLAN_PROMPT), LLMMessage("user", user_text)],
                [],
                model,
            ):
                if isinstance(ev, TextDelta):
                    raw += ev.content
            return self._parse_plan(raw, user_text)
        except Exception:
            return {"objective": user_text, "steps": [{"title": "Objective", "task": user_text, "compute": "research"}]}

    @staticmethod
    def _parse_plan(raw: str, user_text: str) -> dict:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return {"objective": user_text, "steps": [{"title": "Objective", "task": user_text, "compute": "research"}]}
        try:
            plan = json.loads(match.group(0))
            steps = plan.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ValueError("no steps")
            for s in steps:
                s.setdefault("title", "Step")
                s.setdefault("task", user_text)
                s.setdefault("compute", "research")
            plan["objective"] = plan.get("objective") or user_text
            return plan
        except Exception:
            return {"objective": user_text, "steps": [{"title": "Objective", "task": user_text, "compute": "research"}]}

    @staticmethod
    def _render_reports(objective: str, reports: List[dict]) -> str:
        lines = [f"OBJECTIVE: {objective}", ""]
        for r in reports:
            lines.append(f"### {r['title']} (worker role: {r['role']})")
            lines.append(r["report"])
            lines.append("")
        return "\n".join(lines)


async def _emit_safe(emit: EmitFn, event: dict) -> None:
    if emit is None:
        return
    try:
        result = emit(event)
        import asyncio

        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass