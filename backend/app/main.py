from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth
from .agents.commander import Commander
from .computer.runner import ComputerRunner
from .config import Settings, load_settings
from .db import Database
from .providers.base import LLMMessage
from .providers.router import Router
from .providers import build_providers
from .tools.executor import Executor
from .tools.workspace import WorkspaceClient
from .vm import vnc as vnc_bridge
from .vm import websockify_proxy
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
    # Which passphrase this process loaded, from where, and its fingerprint --
    # never the value.  A wrong passphrase, a passphrase that was never loaded
    # and a passphrase the server loaded from somewhere other than the file the
    # operator is reading all produce the same "incorrect passphrase" on the
    # login screen, and this line is what separates them without the operator
    # having to guess.  It goes to the log, which only the operator can read;
    # it is deliberately not on any route.
    _info = auth.describe()
    print(
        "[auth] RAG_AUTH_TOKEN "
        f"loaded={_info.get('loaded')} source={_info.get('source')} "
        f"fingerprint={_info.get('fingerprint') or '(none)'}"
        + (f" duplicate_lines_in_file={_info['duplicate_lines_in_file']}" if _info.get("duplicate_lines_in_file") else "")
        + (" [environment shadows backend/.env]" if _info.get("shadowed_file_value") else ""),
        flush=True,
    )
    if not _info.get("loaded"):
        print(
            "[auth] WARNING: no passphrase is configured, so every route except "
            "/health and /auth/* is refused.  Set RAG_AUTH_TOKEN in backend/.env "
            "or as a Codespaces secret, then re-run codespace/boot.sh.",
            flush=True,
        )
    await _attach_computer()
    yield


app = FastAPI(title="RAG Agents Backend", lifespan=lifespan)
# No CORS middleware on purpose.  The UI and the screen are served from this
# same process, so every request is same-origin and CORS is not needed -- and a
# wildcard here would be actively wrong: `Access-Control-Allow-Origin: *` is
# incompatible with credentialed requests, and re-enabling it would mean
# either dropping the session cookie or reflecting arbitrary origins.  A
# separate frontend origin (Vite in development) is proxied by Vite, not by this
# process, so it does not need an exception here either.


# --- who is allowed in ------------------------------------------------------
# The Quick Tunnel puts this app on the open internet, and the Computer view is
# a live keyboard-driven signed-in browser.  A trycloudflare.com URL is not a
# secret, so every route that could touch the machine, the conversations or the
# screen requires a session.  /health, /auth/* and the static UI stay open: the
# first two are needed to log in, and the third is the login page itself.
# Paths that must work before anybody has logged in: the health probe, the login
# endpoints themselves, and the static assets that make up the login page.  The
# catch-all UI at "/" is public for the same reason -- it *is* the login page.
#
# Deliberately absent: /websockify and /ws/screen.  The screen is the thing worth
# protecting, and /ws/screen has an explicit check in its own handler because HTTP
# middleware does not run for WebSocket upgrades.
_PUBLIC_EXACT = frozenset({"/health", "/auth/login", "/auth/status", "/auth/logout"})
_PUBLIC_PREFIXES = ("/assets/", "/favicon")


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _session_ok(request: Request) -> bool:
    if not auth.enabled():
        # No passphrase configured.  Refuse to serve rather than serve openly:
        # a missing secret should stop the deployment, not silently disable the
        # only thing protecting a signed-in browser.
        return False
    return auth.check_cookie(request.cookies.get("rag_session"))


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Refuse unauthenticated HTTP requests to anything but the login page.

    A Quick Tunnel is a public URL.  Without this, /threads, /file and the
    agent tools would all be open to anyone who learned the link, so the
    check has to live in front of the routes rather than inside each handler --
    a new route added later would otherwise be public by default.
    """
    path = request.url.path
    if _is_public(path):
        return await call_next(request)
    # Fail closed: no passphrase configured means nobody can be authenticated,
    # so the route is refused rather than served.  An unconfigured secret must
    # stop the deployment, not quietly disable the only thing standing between a
    # public tunnel and a signed-in browser.
    if auth.enabled() and _session_ok(request):
        return await call_next(request)
    # The SPA is served from "/", so an unauthenticated visitor is sent to the
    # app itself and the frontend shows the login screen.  Anything else gets a
    # plain 401, which is what a fetch() expects.
    #
    # This list is a *denylist*, which means a route added later is public until
    # somebody remembers to add it here.  Computer control is the reason that is
    # no longer acceptable: /ai/computer/start is a route that moves a real
    # pointer on a real signed-in browser, and an unlisted prefix would put it on
    # the open internet behind a Quick Tunnel.  Anything under /ai belongs in
    # this list, and new API routes should be added to it in the same commit that
    # creates them.
    if path == "/" or not path.startswith(("/threads", "/sysinfo", "/file", "/cdp", "/computer", "/ai", "/screen", "/auth")):
        return await call_next(request)
    return JSONResponse(
        {"ok": False, "error": "authentication required", "auth_required": True},
        status_code=401,
    )


@app.get("/auth/status")
async def auth_status(request: Request):
    """Whether a passphrase is required, and whether this caller has one."""
    return {
        "auth_required": auth.enabled(),
        "authenticated": _session_ok(request),
    }


class LoginBody(BaseModel):
    passphrase: str = ""


@app.post("/auth/login")
async def auth_login(body: LoginBody, request: Request):
    if not auth.enabled():
        return JSONResponse(
            {"ok": False, "error": "no passphrase is configured on the server"},
            status_code=503,
        )
    if not auth.check_passphrase(body.passphrase):
        # Deliberately vague: a message that distinguishes "wrong passphrase"
        # from "no such user" helps someone guessing.
        return JSONResponse({"ok": False, "error": "incorrect passphrase"}, status_code=401)
    return JSONResponse(
        {"ok": True},
        headers={"Set-Cookie": auth.session_cookie(auth._request_is_secure(request))},
    )


@app.post("/auth/logout")
async def auth_logout(request: Request):
    return JSONResponse(
        {"ok": True},
        headers={"Set-Cookie": auth.cleared_cookie(auth._request_is_secure(request))},
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


# --- computer control: the AI driving the real browser ------------------------
#
# Namespaced under /ai/computer rather than added to /computer, because
# /computer/start already means "power the machine on" and "let the model start
# clicking things" is a different operation with a different blast radius.

ai_computer = ComputerRunner(settings, router, db, manager=computer)


class ComputerTaskBody(BaseModel):
    task: str = ""
    thread_id: str = ""


@app.post("/ai/computer/start")
async def ai_computer_start(body: ComputerTaskBody):
    """Hand a task to the computer-control model and return straight away.

    Synchronous on purpose.  A control loop runs for as many model calls and
    screenshots as the task needs; holding this request open would mean the
    browser's own request either times out or blocks every other user of the
    API.  The frontend polls the status route instead.
    """
    task = (body.task or "").strip()
    if not task:
        return {"error": "a task is required"}
    if not settings.openrouter_api_key or not settings.computer_model:
        # Said up front rather than as a mysterious failure three calls in.
        return {
            "error": (
                "computer control is not configured: set OPENROUTER_API_KEY and "
                "OPENROUTER_MODEL in the environment (or backend/.env) and restart"
            )
        }
    if not await computer.is_up():
        return {"error": "the remote computer is not running; start it first"}
    run = await ai_computer.start(task, thread_id=body.thread_id)
    return run.public()


@app.get("/ai/computer/{task_id}")
async def ai_computer_status(task_id: str):
    run = ai_computer.get(task_id)
    if run is None:
        return {"error": "no such computer-control task"}
    return run.public()


@app.post("/ai/computer/{task_id}/stop")
async def ai_computer_stop(task_id: str):
    if not await ai_computer.stop(task_id):
        return {"error": "no such computer-control task"}
    return {"ok": True, "task_id": task_id}


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
async def proxy_file(
    path: str = Query("", alias="path"),
    user_id: str = "",
    download: bool = False,
):
    """Relay a workspace file, or hand it back untouched as an attachment.

    The same route serves both in-app previews and the Download button, so a
    preview never has to be converted, re-encoded or copied to a second URL
    to be downloadable.  ``download=1`` only adds a Content-Disposition header;
    the bytes are the agent's bytes in both cases.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{await _computer_url()}/file", params={"path": path})
            content_type = resp.headers.get("content-type", "application/octet-stream")
            headers = {}
            if download:
                # Derived from the last path segment and stripped of anything
                # that could break out of the header: this value is reflected
                # into Content-Disposition.
                name = re.split(r"[\\/]", path)[-1] or "download"
                name = re.sub(r'[\r\n"\\]', "", name).strip() or "download"
                headers["Content-Disposition"] = f'attachment; filename="{name}"'
            return Response(content=resp.content, media_type=content_type, headers=headers)
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
# Live screen - the real framebuffer over VNC
#
# Production path: the Computer view opens /websockify on the tunnel's own
# origin, which this app proxies to websockify on loopback after checking the
# session cookie.  Nothing below is on that route.
#
# Fallback path: /ws/screen relays raw RFB over this backend so the screen
# still works while developing, or if the tunnel is down.  It is gated the same
# way, so it is a different path and not a weaker one.
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


# Only a real quick-tunnel origin is worth publishing.  The file is written by
# codespace/start-tunnel.sh, but PUBLIC_URL_FILE is configurable and a stale or
# hand-edited value must not be able to aim the viewer somewhere unexpected --
# and "somewhere" is a live keyboard-driven browser, so a bogus origin here is
# not a cosmetic bug.  Requiring the full https://<name>.trycloudflare.com shape
# also means a lookalike such as ....trycloudflare.com.evil.com is rejected.
_TUNNEL_ORIGIN_RE = re.compile(
    r"^https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com$"
)


def _published_url() -> tuple[str, str]:
    """The Quick Tunnel's current public origin, read from disk.

    A Quick Tunnel is assigned a fresh ``trycloudflare.com`` hostname every time
    cloudflared starts and cannot be told what it will be, so there is nothing to
    configure ahead of time.  The tunnel script therefore writes the URL it was
    handed into a file, and this reads it.

    Reading a file on every call -- rather than caching at startup -- is what
    makes a tunnel restart work: the next ``/screen/config`` sees the new
    hostname and the viewer reconnects to it with no rebuild and no redeploy.

    Anything that is not recognisably a quick-tunnel origin returns empty, and
    empty means "use the in-app relay" rather than "send the viewer somewhere
    unexpected".  A loopback URL fails that check too, which is correct: it names
    this machine, not a published tunnel.

    Returns (origin, age_seconds).  age lets the UI explain a stale URL instead
    of quietly showing a screen that has been dead for an hour.
    """
    path = settings.public_url_file
    try:
        raw = open(path, encoding="utf-8").read().strip()
    except OSError:
        return "", 0.0
    # cloudflared can echo http:// in some versions; the public edge is always
    # https, so normalise before validating rather than after.
    if raw.startswith("http://"):
        raw = "https://" + raw[len("http://") :]
    origin = raw.rstrip("/")
    if not _TUNNEL_ORIGIN_RE.match(origin):
        return "", 0.0
    try:
        age = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        age = 0.0
    return origin, age


@app.get("/screen/config")
async def screen_config(request: Request):
    """Where the Computer view should open its WebSocket.

    The public route is the same origin this app is served from, which is what
    makes the session cookie first-party for the WebSocket upgrade: the browser
    has already proved who it is to load the page, and carries the same cookie
    when it opens the screen.  Nothing about the screen can be reached without
    that cookie, which is what makes an unguessable-but-not-secret tunnel URL
    acceptable here.
    """
    origin, age = _published_url()
    # Only advertise a public route we can actually vouch for.
    public = f"{origin}/websockify" if origin else ""
    # The WebSocket() constructor would normalise https->wss for us, but handing
    # the viewer a literal wss:// URL keeps the value honest about what it is and
    # keeps it usable by anything that is not a browser.
    ws_public = public.replace("https://", "wss://", 1) if public else ""
    return {
        "ok": True,
        "mode": "tunnel" if public else "bridge",
        # Same-origin, authenticated by the session cookie.
        "wsUrl": ws_public,
        # Used only in bridge mode; the client appends nothing to it.
        "bridgePath": "/ws/screen",
        # noVNC negotiates the binary subprotocol; websockify serves it.
        "wsProtocols": ["binary"],
        "websockifyPort": computer.websockify_port,
        # Lets the UI say the tunnel moved, or that it has been gone a while.
        "publicOrigin": origin,
        "publicUrlAgeSeconds": round(age, 1),
        "authRequired": auth.enabled(),
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


@app.websocket("/websockify")
async def ws_websockify(websocket: WebSocket):
    """The public screen endpoint: this app -> local websockify -> RFB.

    This is the only path from the internet to the remote Chrome, and it is on
    the same origin as the UI, so the session cookie that let the user load the
    page is presented here too.  A browser cannot add headers to a WebSocket
    handshake, which is exactly why the cookie -- not a bearer token -- is the
    credential.

    websockify on :6080 is never published.  The Quick Tunnel points at this
    process, so the RFB stream and 5900 stay on loopback and the tunnel is the
    only door.
    """
    if not _session_ok(websocket):
        # 1008 = policy violation.  Denying before accept() means the client
        # gets a clean rejection rather than a screen that connects and then
        # hangs, which is indistinguishable from a broken tunnel.
        await websocket.close(code=1008)
        return
    target = websockify_proxy.loopback_websockify_url(computer.websockify_port)
    await websockify_proxy.proxy_websockify(websocket, target)


@app.websocket("/ws/screen")
async def ws_screen(websocket: WebSocket):
    """Fallback relay: a binary WebSocket straight to the VNC server.

    Used when no Quick Tunnel is publishing a public URL, e.g. during local
    development.  The socket carries raw RFB, so status and control stay on the
    HTTP routes above and the client never has to demultiplex two protocols.
    """
    if not _session_ok(websocket):
        await websocket.close(code=1008)
        return
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
    # The app session is a control channel: it starts the computer, runs the
    # agents and reads their conversations.  On a public tunnel it needs the
    # same session cookie as everything else.
    if not _session_ok(websocket):
        await websocket.close(code=1008)
        return
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
