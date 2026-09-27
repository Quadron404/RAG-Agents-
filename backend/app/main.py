from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents.commander import Commander
from .config import Settings, load_settings
from .db import Database
from .providers.base import LLMMessage
from .providers.router import Router
from .providers import build_providers
from .tools.executor import Executor
from .tools.workspace import WorkspaceClient
from .vm import vnc as vnc_bridge
from .workspace import WorkspaceManager

settings = load_settings()
db = Database(os.path.join(settings.data_dir, "rag.db"))
providers = build_providers(settings)
router = Router(providers, settings)
computer = WorkspaceManager(settings)
executor = Executor(settings)
commander = Commander(router, executor)


def _client() -> WorkspaceClient:
    return WorkspaceClient(settings.workspace_base_url, settings)


async def _attach_computer() -> None:
    """Point the agent tools at the computer if it is already running.

    Without this, a backend restart would leave the tools running locally until
    someone pressed the power button again, which reads as "the agents stopped
    being able to browse".
    """
    if await computer.is_up():
        executor.use_computer(_client())


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _attach_computer()
    yield


app = FastAPI(title="RAG Agents Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class NewThreadBody(BaseModel):
    user_id: str
    title: str = "New conversation"


class SendBody(BaseModel):
    user_id: str
    text: str


class ComputerBody(BaseModel):
    user_id: str = "default"


@app.get("/health")
async def health():
    return {
        "ok": True,
        "computer": computer.describe(),
        "providers": list(providers.keys()),
        "commander": settings.commander_provider,
        "worker": settings.worker_provider,
    }


@app.post("/threads")
async def create_thread(body: NewThreadBody):
    thread_id = db.create_thread(body.user_id, body.title)
    return {"thread_id": thread_id, "user_id": body.user_id, "title": body.title}


@app.get("/threads")
async def list_threads(user_id: str):
    return {"threads": db.list_threads(user_id)}


@app.get("/threads/{thread_id}/messages")
async def get_messages(thread_id: str):
    if not db.get_thread(thread_id):
        return {"error": "thread not found"}
    return {"messages": db.list_messages(thread_id)}


@app.post("/threads/{thread_id}/send")
async def send_message(thread_id: str, body: SendBody):
    if not db.get_thread(thread_id):
        return {"error": "thread not found"}
    events: list = []
    db.add_message(thread_id, "user", body.text, agent="user")
    await run_commander_on_thread(thread_id, body.user_id, body.text, events.append)
    return {"events": events}


@app.post("/computer/start")
async def computer_start(body: ComputerBody):
    """Power the computer on.  Idempotent: the agent supervises the stack."""
    result = await computer.start()
    if result.get("ok"):
        executor.use_computer(_client())
    return result


@app.post("/computer/stop")
async def computer_stop(body: ComputerBody):
    """Close the browser session, leaving the screen itself available."""
    result = await computer.stop()
    if result.get("ok"):
        executor.use_computer(_client())
    return result


@app.get("/computer")
async def computer_list():
    return {"computers": computer.list_computers()}


async def _computer_url() -> str:
    return settings.workspace_base_url


@app.get("/sysinfo")
async def proxy_sysinfo(user_id: str = ""):
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{await _computer_url()}/sysinfo")
            return JSONResponse(content=resp.json())
    except Exception as exc:
        return JSONResponse(content={"ok": False, "error": f"computer unreachable: {exc}"}, status_code=502)


@app.get("/file")
async def proxy_file(path: str = Query("", alias="path"), user_id: str = ""):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{await _computer_url()}/file", params={"path": path})
            content_type = resp.headers.get("content-type", "application/octet-stream")
            return Response(content=resp.content, media_type=content_type)
    except Exception as exc:
        return Response(content=str(exc).encode(), media_type="text/plain", status_code=502)


@app.get("/cdp/status")
async def proxy_cdp_status(user_id: str = ""):
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{await _computer_url()}/cdp/status")
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse(content={"ok": False, "error": f"computer unreachable: {exc}"}, status_code=502)


# ---------------------------------------------------------------------------
# Live screen — the real framebuffer over VNC
#
# Production path: the Computer view connects straight to
#   wss://computer.<domain>/websockify
# which Cloudflare Access authenticates and the tunnel forwards to
# websockify on loopback.  Nothing below is on that route.
#
# Fallback path: /ws/screen relays raw RFB over this backend so the screen
# still works while developing, or if the tunnel is down.
# ---------------------------------------------------------------------------


def _screen_endpoint() -> Optional[tuple]:
    """Loopback host:port of the framebuffer's RFB server, or None.

    One seam for every consumer -- the config route, the health probe and the
    relay -- so they can never disagree about where the screen lives.  Tests
    substitute this to point the relay at a canned server.
    """
    return computer.screen_target


def _screen_payload(listening: bool, display: dict, note: str = "") -> dict:
    target = _screen_endpoint()
    host, port = target if target else ("", 0)
    return {
        "ok": True,
        "host": host,
        "port": port,
        "listening": listening,
        "display": display,
        "note": note,
    }


@app.get("/screen/config")
async def screen_config():
    """Where the Computer view should open its WebSocket.

    This is the one place that knows the public route exists, so the hostname
    stays a server-side setting.  When COMPUTER_WS_URL is unset the client
    falls back to this backend's own relay, which is correct for local
    development and never correct for production.
    """
    public = settings.computer_ws_url
    return {
        "ok": True,
        "mode": "tunnel" if public else "bridge",
        # The authenticated, Cloudflare-proxied endpoint.
        "wsUrl": public,
        # Used only in bridge mode; the client appends nothing to it.
        "bridgePath": "/ws/screen",
        # noVNC negotiates the binary subprotocol; websockify serves it.
        "wsProtocols": ["binary"],
        "websockifyPort": computer.websockify_port,
    }


@app.get("/screen/status")
async def screen_status():
    """Real health of the computer's display, for the UI's status line.

    Only a local TCP probe and a status call into the agent: no frame ever
    passes through the backend or the model.
    """
    target = _screen_endpoint()
    if target is None:
        return _screen_payload(False, {}, note="the screen is disabled")
    host, port = target
    listening = await vnc_bridge.tcp_listening(host, port)
    display: dict = await computer.display_status()
    if display and not display.get("ok", True):
        listening = False
    if not listening:
        # The agent supervises the stack itself; nudge it so a viewer that
        # arrives before the desktop is up still ends up on a live screen.
        asyncio.create_task(_ensure_display())
    return _screen_payload(listening, display)


async def _ensure_display() -> None:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(f"{await _computer_url()}/display/ensure")
    except Exception:
        pass


@app.post("/screen/ensure")
async def screen_ensure():
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{await _computer_url()}/display/ensure")
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse(content={"ok": False, "error": f"computer unreachable: {exc}"}, status_code=502)


@app.post("/screen/browser/restart")
async def screen_browser_restart():
    """Restart the remote browser; the screen then follows the real state."""
    result = await computer.restart_browser()
    if not result.get("ok"):
        return JSONResponse(content=result, status_code=502)
    return JSONResponse(content=result)


@app.websocket("/ws/screen")
async def ws_screen(websocket: WebSocket):
    """Fallback relay: a binary WebSocket straight to the VNC server.

    Used only when no public COMPUTER_WS_URL is configured.  The socket carries
    raw RFB, so status and control stay on the HTTP routes above and the client
    never has to demultiplex two protocols.
    """
    target = _screen_endpoint()
    if target is None:
        await websocket.close(code=1008)
        return
    offered = websocket.headers.get("sec-websocket-protocol", "")
    subprotocol = "binary" if "binary" in offered else None
    await websocket.accept(subprotocol=subprotocol)
    host, port = target
    await vnc_bridge.relay(websocket, host, port)


@app.websocket("/ws/{user_id}")
async def ws_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    await websocket.send_json({"type": "connected", "user_id": user_id})
    active_thread = None
    try:
        while True:
            data = await websocket.receive_json()
            kind = data.get("type")

            if kind == "message":
                text = data.get("text", "")
                thread_id = data.get("thread_id")
                if not text.strip():
                    await websocket.send_json({"type": "error", "error": "empty message"})
                    continue
                if not thread_id or not db.get_thread(thread_id):
                    thread_id = db.create_thread(user_id, text[:50])
                    await websocket.send_json({"type": "thread", "thread_id": thread_id})
                active_thread = thread_id
                db.add_message(thread_id, "user", text, agent="user")
                await websocket.send_json({"type": "user_ack", "thread_id": thread_id})
                await run_commander_on_thread(thread_id, user_id, text, _ws_emit(websocket))

            elif kind == "computer_status":
                # "Is the computer there?"  The agent's own answer is the
                # truth; the display probe is only a hint for the UI label.
                up = await computer.is_up()
                display = await computer.display_status() if up else {}
                await websocket.send_json(
                    {
                        "type": "computer",
                        "running": bool(display.get("chromium")) if display else up,
                        "reachable": up,
                    }
                )

            elif kind == "screen":
                # The Browser tab's agent-driven still, captured by the agent
                # through Chrome's own debugging port.  The authoritative view
                # of the machine is the live screen, not this image.
                shot = await _client().screenshot()
                await websocket.send_json({"type": "screen", "image": shot})

            elif kind == "computer_start":
                result = await computer.start()
                await websocket.send_json({"type": "computer", "result": result})
                if result.get("ok"):
                    executor.use_computer(_client())

            elif kind == "computer_stop":
                result = await computer.stop()
                await websocket.send_json({"type": "computer", "result": result})
                executor.use_computer(_client())

            elif kind == "tool":
                tool = str(data.get("tool", ""))
                args = data.get("args") or {}
                timeout = int(data.get("timeout", 90))
                rid = str(data.get("id", ""))
                if not tool:
                    await websocket.send_json({"type": "tool_result", "id": rid, "error": "no tool specified"})
                    continue
                output, error, title, url, image, fmt = "", "", "", "", "", ""
                try:
                    async for ev in executor.run(tool, args):
                        if ev.get("type") == "result":
                            output = ev.get("output", "") or ""
                            error = ev.get("error", "") or ""
                            title = ev.get("title", "") or ""
                            url = ev.get("url", "") or ""
                            image = ev.get("image", "") or ""
                            fmt = ev.get("format", "") or ""
                except Exception as exc:
                    error = f"tool failed: {exc}"
                await websocket.send_json({
                    "type": "tool_result",
                    "id": rid,
                    "tool": tool,
                    "output": output,
                    "error": error,
                    "title": title,
                    "url": url,
                    "image": image,
                    "format": fmt,
                })

            else:
                await websocket.send_json({"type": "error", "error": f"unknown type: {kind}"})
    except WebSocketDisconnect:
        pass


async def run_commander_on_thread(thread_id: str, user_id: str, text: str, emit) -> None:
    memory = db.recall(user_id)
    context: list = []
    for m in db.list_messages(thread_id, tail=40):
        if m["role"] == "user":
            context.append(LLMMessage("user", m["content"]))
        elif m["role"] == "assistant" and m["agent"] == "commander":
            context.append(LLMMessage("assistant", m["content"]))
    if memory:
        context.append(LLMMessage("system", "Long-term memory:\n" + json.dumps(memory, indent=2)))

    result = await commander.run(text, context, emit)
    db.add_message(thread_id, "assistant", result["text"], agent="commander")
    for report in result.get("reports", []):
        title = report.get("title", "")
        role = report.get("role", "")
        agent = report.get("agent", "")
        db.add_message(thread_id, "assistant", report["report"], agent=f"worker:{role}:{title}")


def _ws_emit(websocket: WebSocket):
    async def emit(event: dict):
        try:
            await websocket.send_json({"type": "event", "event": event})
        except Exception:
            pass
    return emit


def _frontend_dir() -> str:
    """The Vite build to serve, if there is one.

    Serving the app from this same origin as the screen matters: the Cloudflare
    Access cookie is first-party for the app, so the noVNC WebSocket upgrade
    carries it without any cross-origin or third-party-cookie workaround.
    """
    candidates = []
    if settings.frontend_dist:
        candidates.append(settings.frontend_dist)
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    candidates.append(os.path.join(repo, "Frontend", "dist"))
    for cand in candidates:
        path = cand if os.path.isabs(cand) else os.path.join(repo, cand)
        if os.path.isfile(os.path.join(path, "index.html")):
            return path
    return ""


_ui_dir = _frontend_dir()
if _ui_dir:
    app.mount("/", StaticFiles(directory=_ui_dir, html=True), name="ui")
else:
    # Fall back to the bundled prototype so the server is never a bare 404.
    _static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    if os.path.isdir(_static_dir):
        app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
