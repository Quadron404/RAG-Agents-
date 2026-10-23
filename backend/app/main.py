from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents import memory as agent_memory
from .agents import routines as agent_routines
from .agents import summaries as agent_summaries
from .agents.commander import Commander
from .agents.conversation import assemble_context
from .auth import google as google_oauth
from .auth.deps import INTERNAL_HEADER, AuthService, Principal
from .auth.sessions import COOKIE_NAME
from .computer.runner import ComputerRunner
from .config import Settings, load_settings
from .db import Database
from .email import gmail as gmail_connection
from .providers.router import (
    Router,
    computer_model_for,
    computer_provider_info,
    computer_providers,
)
from .providers import build_providers
from .restricted.runtime import RestrictedRuntime
from .routines import RoutineContext, reset_current as reset_routine_context
from .routines import set_current as set_routine_context
from .routines.client import RoutineClient
from .tools.executor import Executor
from .tools.workspace import WorkspaceClient
from .vm import vnc as vnc_bridge
from .vm import websockify_proxy
from .workspace import WorkspaceManager

settings = load_settings()
db = Database(os.path.join(settings.data_dir, "rag.db"))
auth = AuthService(settings, db)
providers = build_providers(settings)
router = Router(providers, settings)
computer = WorkspaceManager(settings)
# Ordinary agents get a browser of their own: a separate, logged-out browser
# behind an owner-controlled allowlist and a network gateway.  It is wired into
# the executor below so the network tools cannot leave by any other path.
restricted = RestrictedRuntime(settings)
executor = Executor(settings, restricted=restricted, db=db)
#: Routines are ordinary, non-computer tasks by definition, so they run on an
#: executor with no computer attached.  A scheduled task can never wander onto
#: the personal Chrome or the shared desktop.
routine_executor = Executor(settings, restricted=restricted, db=db)
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
# No CORS middleware on purpose.  The UI and the screen are served from this
# same process, so every request is same-origin and CORS is not needed -- and a
# wildcard here would be actively wrong: `Access-Control-Allow-Origin: *` is
# incompatible with credentialed requests, and re-enabling it would mean
# either dropping the session cookie or reflecting arbitrary origins.  A
# separate frontend origin (Vite in development) is proxied by Vite, not by this
# process, so it does not need an exception here either.


# ---------------------------------------------------------------------------
# Authentication and the internal gateway.
#
# The previous passphrase gate was removed and the app was briefly served to
# anyone who could reach the Quick Tunnel, which exposed /file, /ai/*,
# /websockify and the Computer view to whoever found the link.  That is undone
# here with a real Google identity, server-side sessions and a shared internal
# token that only the Cloudflare Worker knows.
#
# Two independent checks guard every protected route:
#   1. the request must have come through the trusted gateway
#      (RAG_INTERNAL_TOKEN) once one is configured; and
#   2. it must carry a valid session cookie mapping to a stored user.
# A client-supplied user_id is never used for authorization again.
# ---------------------------------------------------------------------------


def _principal(request: Request) -> Principal:
    return auth.require_principal(request)


def _redirect_uri(request: Request, path: str) -> str:
    """The exact callback URI Google must have registered.

    ``PUBLIC_BASE_URL`` wins when set, because behind the Worker the request's
    own scheme/host is the tunnel's, not the stable public one.  Falling back to
    the request origin keeps local development working without configuration.
    """
    if settings.public_base_url:
        return f"{settings.public_base_url}{path}"
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


def _gmail_status(user_id: str) -> dict:
    """The mailbox state for one user, safe to send to the browser.

    Only the address and a coarse status leave here -- never a token, and never
    the encrypted columns.  ``autoReply`` is carried alongside because it is a
    per-connection preference the Settings panel reads.
    """
    connection = db.get_gmail_connection(user_id)
    summary = gmail_connection.connection_summary(connection)
    summary["autoReply"] = bool(connection and connection.get("auto_reply"))
    return summary


def _is_secure(request: Request) -> bool:
    if settings.public_base_url.startswith("https://"):
        return True
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
        path="/",
    )


@app.get("/auth/session")
async def auth_session(request: Request):
    """Who is signed in, whether Gmail is connected, and the CSRF token.

    Public on purpose: the frontend calls it before it knows whether anyone is
    signed in, and it returns an anonymous shape rather than a 401 in that case.
    It never reveals another user's address or connection status.
    """
    principal = auth.optional_principal(request)
    if not principal:
        return {"authenticated": False, "user": None, "gmail": {"connected": False}}
    user = principal.user
    gmail = _gmail_status(principal.user_id)
    return {
        "authenticated": True,
        "csrf": principal.session.get("csrf", ""),
        "user": {
            "id": user["id"],
            "email": user.get("email", ""),
            "name": user.get("name", ""),
            "picture": user.get("picture", ""),
            "timezone": user.get("timezone", "UTC"),
        },
        "gmail": gmail,
    }


@app.get("/auth/login")
async def auth_login(request: Request, next: str = "/"):
    """Begin the login-only Google flow (``openid email profile``)."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    verifier, _ = google_oauth.pkce_pair()
    state = google_oauth.new_state()
    nonce = google_oauth.new_nonce()
    redirect_uri = _redirect_uri(request, "/auth/callback")
    db.create_oauth_state(state, "login", "", verifier, nonce, redirect_uri)
    url = google_oauth.build_authorization_url(
        settings=settings,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        verifier=verifier,
        mode="login",
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Exchange the code, validate the id_token, and rotate into a session."""
    error = request.query_params.get("error")
    if error:
        return RedirectResponse("/?auth=denied")
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if not state or not code:
        raise HTTPException(status_code=400, detail="missing state or code")
    record = db.consume_oauth_state(state)
    if not record or record.get("mode") != "login":
        db.audit_login("", "", "rejected", "invalid_or_reused_state")
        raise HTTPException(status_code=400, detail="invalid or expired sign-in state")

    tokens = await google_oauth.exchange_code(
        settings=settings,
        code=code,
        redirect_uri=str(record["redirect_uri"]),
        verifier=str(record["code_verifier"]),
    )
    id_token = str(tokens.get("id_token") or "")
    if not id_token:
        db.audit_login("", "", "rejected", "no_id_token")
        raise HTTPException(status_code=400, detail="no identity token returned")
    jwks = await google_oauth.fetch_jwks(settings)
    claims = google_oauth.verify_login_token(settings, id_token, jwks, str(record["nonce"]))
    user = db.upsert_user(
        str(claims["sub"]),
        email=str(claims.get("email") or ""),
        email_verified=bool(claims.get("email_verified")),
        name=str(claims.get("name") or ""),
        picture=str(claims.get("picture") or ""),
    )
    db.audit_login(str(claims["sub"]), str(user["id"]), "success", "")
    session = auth.sessions.create(str(user["id"]))
    response = RedirectResponse("/")
    _set_session_cookie(response, str(session["token"]), request)
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    token = auth.cookie_token(request)
    auth.sessions.revoke(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/auth/gmail/status")
async def gmail_status(request: Request):
    """The signed-in user's real mailbox connection state.

    Separate from ``/auth/session`` so the edge can answer the session quickly
    while the backend -- which owns the connection record -- remains the single
    source of truth for whether a mailbox is connected.  Never returns a token.
    """
    principal = _principal(request)
    return _gmail_status(principal.user_id)


@app.get("/auth/gmail/connect")
async def gmail_connect(request: Request):
    """Start the separate Gmail consent for the signed-in user."""
    principal = auth.require_principal(request)
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    verifier, _ = google_oauth.pkce_pair()
    state = google_oauth.new_state()
    nonce = google_oauth.new_nonce()
    redirect_uri = _redirect_uri(request, "/auth/gmail/callback")
    db.create_oauth_state(state, "gmail", principal.user_id, verifier, nonce, redirect_uri)
    url = google_oauth.build_authorization_url(
        settings=settings,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        verifier=verifier,
        mode="gmail",
        login_hint=str(principal.user.get("email") or ""),
    )
    return RedirectResponse(url)


@app.get("/auth/gmail/callback")
async def gmail_callback(request: Request):
    """Bind a granted mailbox to the SAME identity that owns the session."""
    principal = auth.require_principal(request)
    error = request.query_params.get("error")
    if error:
        return RedirectResponse("/?gmail=denied")
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    record = db.consume_oauth_state(state) if state else None
    if not record or record.get("mode") != "gmail" or str(record.get("user_id")) != principal.user_id:
        raise HTTPException(status_code=400, detail="invalid or expired Gmail authorization state")
    if not code:
        raise HTTPException(status_code=400, detail="missing authorization code")

    tokens = await google_oauth.exchange_code(
        settings=settings,
        code=code,
        redirect_uri=str(record["redirect_uri"]),
        verifier=str(record["code_verifier"]),
    )
    id_token = str(tokens.get("id_token") or "")
    if not id_token:
        raise HTTPException(status_code=400, detail="no identity token returned")
    jwks = await google_oauth.fetch_jwks(settings)
    claims = google_oauth.verify_login_token(settings, id_token, jwks, str(record["nonce"]))
    # The binding check: consent must have been granted by the same Google
    # subject that owns the session.  A different account is a hard rejection.
    if str(claims["sub"]) != str(principal.user["google_sub"]):
        db.audit_login(str(claims["sub"]), principal.user_id, "rejected", "gmail_subject_mismatch")
        return RedirectResponse("/?gmail=wrong_account")

    refresh = str(tokens.get("refresh_token") or "")
    if not refresh:
        # Google omits refresh_token when the grant already exists; treat as
        # needing explicit reconsent rather than silently doing nothing.
        return RedirectResponse("/?gmail=no_refresh_token")
    db.save_gmail_connection(
        user_id=principal.user_id,
        google_sub=str(claims["sub"]),
        email=str(claims.get("email") or ""),
        scope=str(tokens.get("scope") or " ".join(google_oauth.GMAIL_FULL_SCOPES)),
        refresh_token_enc=auth.encrypt_secret(refresh),
        access_token_enc=auth.encrypt_secret(str(tokens.get("access_token") or "")),
        token_expiry=time.time() + float(tokens.get("expires_in") or 0),
    )
    return RedirectResponse("/?gmail=connected")


@app.post("/auth/gmail/disconnect")
async def gmail_disconnect(request: Request):
    """Forget the mailbox and revoke the grant at Google.

    The remote revocation is best-effort: the local record is removed either way,
    so a network failure cannot make "disconnect" appear to have failed.  The
    refresh token is read only to hand to Google's revoke endpoint and is never
    logged.
    """
    principal = _principal(request)
    connection = db.get_gmail_connection(principal.user_id)
    if connection:
        refresh = auth.decrypt_secret(str(connection.get("refresh_token_enc") or ""))
        if refresh:
            try:
                await google_oauth.revoke_token(settings=settings, token=refresh)
            except Exception:  # noqa: BLE001 - revocation is best-effort
                pass
    db.delete_gmail_connection(principal.user_id)
    return {"ok": True, "gmail": _gmail_status(principal.user_id)}


class NewThreadBody(BaseModel):
    title: str = "New conversation"


class SendBody(BaseModel):
    text: str


class RoutineExecuteBody(BaseModel):
    routine_id: str = ""
    routine_run_id: str = ""
    idempotency_key: str = ""
    owner_id: str = ""
    owner_sub: str = ""
    agent_id: str = ""
    name: str = ""
    task_prompt: str = ""
    scheduled_for: int = 0


class ComputerBody(BaseModel):
    pass


@app.get("/health")
async def health():
    # `_agent_up` is only ever written by a probe, and `describe()` just reads
    # it back.  Reporting that stale bit is how /health said agent_up=false for
    # a whole session after a restart raced the agent coming up -- and would say
    # true forever after the agent died.  Probing here makes the field answer
    # "is the agent reachable now", which is what every caller reads it as.
    await computer.is_up()
    return {
        "ok": True,
        "computer": computer.describe(),
        "providers": list(providers.keys()),
        "agent": {"provider": settings.agent_provider, "model": settings.agent_model},
        "commander": settings.commander_provider,
        "worker": settings.worker_provider,
        "restricted": restricted.describe(),
    }


@app.post("/internal/summaries/run")
async def internal_summaries_run(request: Request):
    """One scheduler tick: prepare and execute due conversation summaries.

    This is the seam the external scheduler drives; there is no timer inside the
    process.  Guarded by the shared internal token when one is configured (the
    gateway is the caller), and otherwise by a signed-in session so local
    development still works without opening an unauthenticated endpoint.
    """
    if auth.internal_configured:
        if not auth.internal_token_valid(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="internal authentication required")
    else:
        _principal(request)
    results = await agent_summaries.run_due_summaries(db, router, settings)
    return {"ok": True, "results": results}


#: The durable-job kind for one routine execution.  The job id is derived from
#: the edge's idempotency key, so a run the edge dispatches twice -- because a
#: lease expired, or a rename raced a retry -- is executed at most once.
ROUTINE_JOB_KIND = "routine.run"
#: Delay before retrying a routine whose provider asked us to come back later.
ROUTINE_RETRY_DELAY_SECONDS = 120.0


@app.post("/internal/routines/execute")
async def internal_routines_execute(request: Request, body: RoutineExecuteBody):
    """Execute one due routine, exactly once, and report the real result.

    Called by the Worker's scheduler, so it is guarded by the shared internal
    token.  A run is only ever answered ``ok`` with the text the task actually
    produced; a rate limit or outage answers retryable so the edge delays the
    run, and a terminal failure is reported as such rather than hidden.
    """
    if auth.internal_configured:
        if not auth.internal_token_valid(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="internal authentication required")
    else:
        _principal(request)

    job_id = f"routine.run:{body.idempotency_key or body.routine_run_id or body.routine_id}"
    existing = db.get_job(job_id)
    if existing and existing.get("status") == "done":
        return {"ok": True, "result": existing.get("result") or "", "deduplicated": True}

    db.enqueue_job(
        ROUTINE_JOB_KIND,
        body.owner_id,
        {
            "routine_id": body.routine_id,
            "routine_run_id": body.routine_run_id,
            "task_prompt": body.task_prompt,
        },
        job_id=job_id,
    )
    if not db.start_job(job_id, lease_seconds=600.0):
        current = db.get_job(job_id)
        if current and current.get("status") == "done":
            return {"ok": True, "result": current.get("result") or "", "deduplicated": True}
        # Another dispatcher holds a live lease on this run.
        return {"ok": False, "retryable": True, "error_code": "in_progress"}

    try:
        outcome = await agent_routines.run_routine_task(
            router, settings, routine_executor, body.model_dump()
        )
    except Exception as exc:
        retryable, code = agent_routines.classify_failure(exc)
        if retryable:
            db.requeue_job(job_id, delay_seconds=ROUTINE_RETRY_DELAY_SECONDS, error_code=code)
        else:
            db.complete_job(job_id, "failed", error_code=code)
        return {
            "ok": False,
            "retryable": retryable,
            "error_code": code,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if outcome.get("ok"):
        db.complete_job(job_id, "done", result=str(outcome.get("result") or ""))
        return {"ok": True, "result": outcome.get("result") or ""}
    code = str(outcome.get("error_code") or "failed")
    db.complete_job(job_id, "failed", error_code=code)
    return {"ok": False, "retryable": False, "error_code": code}


@app.post("/threads")
async def create_thread(request: Request, body: NewThreadBody):
    # The owner is the session, never the body.  Any client-supplied user_id is
    # ignored because the model no longer exists on the request body.
    principal = _principal(request)
    thread_id = db.create_thread(principal.user_id, body.title)
    return {"thread_id": thread_id, "user_id": principal.user_id, "title": body.title}


@app.get("/threads")
async def list_threads(request: Request):
    principal = _principal(request)
    return {"threads": db.list_threads(principal.user_id)}


# ---------------------------------------------------------------------------
# Routines, browser-facing.
#
# The authority for a routine is the edge: the Cloudflare Worker owns the D1
# row, the recurrence clock and the run ledger, because a schedule has to keep
# firing while this backend -- and the Codespace it runs on -- is stopped.  The
# agent already manages routines through chat; these routes are the same surface
# for the UI, so a person can see what is scheduled without having to ask.
#
# They add no new authority.  The browser is never handed the shared internal
# token -- that has to stay server-side -- so the signed-in session is turned
# into the one key the edge understands, the caller's Google subject, and this
# process presents the internal token on the user's behalf.  When the edge is
# not configured, or the account has no routine identity, every route answers
# ``available: false`` with a reason, so the UI can say so plainly instead of
# rendering an empty list that reads as "you have no routines".
# ---------------------------------------------------------------------------


def _routine_owner_sub(principal: Principal) -> str:
    # The Google subject is what the edge keys routines on; it is the identity
    # the Worker asserted for this session and can never come from the client.
    return str(principal.user.get("google_sub") or "")


@app.get("/routines")
async def list_routines(request: Request):
    principal = _principal(request)
    client = RoutineClient.from_settings(settings)
    if client is None:
        return {
            "ok": False,
            "available": False,
            "error": "Routines are not configured on this deployment.",
            "routines": [],
        }
    owner_sub = _routine_owner_sub(principal)
    if not owner_sub:
        return {
            "ok": False,
            "available": False,
            "error": "This account has no routine identity.",
            "routines": [],
        }
    result = await client.list_for(owner_sub)
    body = result.get("body") or {}
    if result.get("status") != 200 or not body.get("ok"):
        return {
            "ok": False,
            "available": True,
            "error": str(body.get("error") or "The routine service did not answer."),
            "routines": [],
        }
    return {"ok": True, "available": True, "routines": body.get("routines") or []}


@app.get("/routines/{routine_id}/runs")
async def routine_runs(request: Request, routine_id: str, limit: int = 20):
    principal = _principal(request)
    client = RoutineClient.from_settings(settings)
    if client is None:
        return {
            "ok": False,
            "available": False,
            "error": "Routines are not configured on this deployment.",
            "runs": [],
        }
    owner_sub = _routine_owner_sub(principal)
    if not owner_sub:
        return {"ok": False, "available": False, "error": "This account has no routine identity.", "runs": []}
    result = await client.runs(routine_id, owner_sub, limit=max(1, min(limit, 100)))
    body = result.get("body") or {}
    if result.get("status") != 200 or not body.get("ok"):
        return {
            "ok": False,
            "available": True,
            "error": str(body.get("error") or "The routine service did not answer."),
            "runs": [],
        }
    return {"ok": True, "available": True, "runs": body.get("runs") or []}


@app.post("/routines/{routine_id}/pause")
async def pause_routine(request: Request, routine_id: str):
    return await _routine_lifecycle(lambda client, sub: client.pause(routine_id, sub), request)


@app.post("/routines/{routine_id}/resume")
async def resume_routine(request: Request, routine_id: str):
    return await _routine_lifecycle(lambda client, sub: client.resume(routine_id, sub), request)


@app.delete("/routines/{routine_id}")
async def delete_routine(request: Request, routine_id: str):
    return await _routine_lifecycle(lambda client, sub: client.delete(routine_id, sub), request)


async def _routine_lifecycle(call, request: Request) -> dict:
    """Run one pause/resume/delete against the edge and normalize the answer.

    All three are the same shape: resolve the caller to their Google subject,
    ask the edge to act on that owner's routine, and report success with the
    updated row (or the edge's own error) rather than guessing at the outcome.
    """
    principal = _principal(request)
    client = RoutineClient.from_settings(settings)
    if client is None:
        return {"ok": False, "available": False, "error": "Routines are not configured on this deployment."}
    owner_sub = _routine_owner_sub(principal)
    if not owner_sub:
        return {"ok": False, "available": False, "error": "This account has no routine identity."}
    result = await call(client, owner_sub)
    body = result.get("body") or {}
    if result.get("status") != 200 or not body.get("ok"):
        return {
            "ok": False,
            "available": True,
            "error": str(body.get("error") or "The routine service did not answer."),
        }
    return {"ok": True, "available": True, "routine": body.get("routine")}


@app.get("/threads/{thread_id}/messages")
async def get_messages(request: Request, thread_id: str):
    principal = _principal(request)
    if not db.thread_owned_by(thread_id, principal.user_id):
        return {"error": "thread not found"}
    return {"messages": db.list_messages(thread_id)}


@app.post("/threads/{thread_id}/send")
async def send_message(request: Request, thread_id: str, body: SendBody):
    principal = _principal(request)
    if not db.thread_owned_by(thread_id, principal.user_id):
        return {"error": "thread not found"}
    events: list = []
    user_message_id = db.add_message(thread_id, "user", body.text, agent="user")
    await run_commander_on_thread(
        thread_id, principal.user_id, body.text, events.append, user_message_id=user_message_id
    )
    return {"events": events}


@app.post("/computer/start")
async def computer_start(request: Request, body: ComputerBody):
    """Power the computer on.  Idempotent: the agent supervises the stack."""
    _principal(request)
    result = await computer.start()
    if result.get("ok"):
        executor.use_computer(_client())
    return result


@app.post("/computer/stop")
async def computer_stop(request: Request, body: ComputerBody):
    """Close the browser session, leaving the screen itself available."""
    _principal(request)
    result = await computer.stop()
    if result.get("ok"):
        executor.use_computer(_client())
    return result


@app.get("/computer")
async def computer_list(request: Request):
    _principal(request)
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
    provider: str = ""


class ComputerProviderBody(BaseModel):
    provider: str = ""


@app.get("/ai/computer/providers")
async def ai_computer_providers(request: Request):
    _principal(request)
    """The providers a computer-control run may be pointed at.

    Name, display label, model and whether a key is present -- and nothing
    else.  The browser needs to render a selector and explain why one option
    will fail, which needs none of the key; a payload carrying it would be a
    secret shipped to every client for no benefit.  Built from the same
    ComputerProviderInfo the router resolves with, so what the selector shows
    and what the loop will do cannot disagree.
    """
    return {
        "providers": [
            {"name": i.name, "label": i.label, "model": i.model, "configured": i.configured}
            for i in computer_providers(settings)
        ],
        "default": settings.computer_provider,
    }


@app.post("/ai/computer/start")
async def ai_computer_start(request: Request, body: ComputerTaskBody):
    _principal(request)
    """Hand a task to the computer-control model and return straight away.

    Synchronous on purpose.  A control loop runs for as many model calls and
    screenshots as the task needs; holding this request open would mean the
    browser's own request either times out or blocks every other user of the
    API.  The frontend polls the status route instead.
    """
    task = (body.task or "").strip()
    if not task:
        return {"error": "a task is required"}
    # Which provider answers the first request.  Validated against the same
    # closed list the selector is built from, so an unknown name is refused
    # here rather than becoming a provider-less run further in.
    provider = (body.provider or settings.computer_provider or "").strip()
    if provider not in {i.name for i in computer_providers(settings)}:
        return {"error": f"unknown computer provider {provider!r}"}
    info = computer_provider_info(settings, provider)
    if not info.configured:
        # Named, so the reader knows which one to go and configure.  "computer
        # control is not configured" with no subject is a puzzle; the two
        # providers are configured independently and either can be the missing
        # one.
        return {"error": f"{info.label} is not configured"}
    if not computer_model_for(settings, provider):
        return {"error": f"{info.label} is not configured: no model configured"}
    if not await computer.is_up():
        return {"error": "the remote computer is not running; start it first"}
    run = await ai_computer.start(task, thread_id=body.thread_id, provider=provider)
    return run.public()


@app.post("/ai/computer/{task_id}/provider")
async def ai_computer_set_provider(request: Request, task_id: str, body: ComputerProviderBody):
    _principal(request)
    """Point the next request at a different provider, keeping the run.

    The task, the recorded history and the step count are all untouched: only
    who answers the next call changes.  The conversation is not replayed
    through the new provider and it is not summarised for it either -- the next
    request carries the same complete canonical history, because the history is
    rebuilt from the recorded events rather than kept in either provider.

    Takes effect on the next request.  A call already in flight is not
    recalled, so the trace keeps naming the provider that actually answered.
    """
    provider = (body.provider or "").strip()
    if provider not in {i.name for i in computer_providers(settings)}:
        return {"error": f"unknown computer provider {provider!r}"}
    info = computer_provider_info(settings, provider)
    if not info.configured:
        return {"error": f"{info.label} is not configured"}
    if not computer_model_for(settings, provider):
        return {"error": f"{info.label} is not configured: no model configured"}
    try:
        run = ai_computer.set_provider(task_id, provider)
    except KeyError:
        return {"error": "no such computer-control task"}
    return {"ok": True, "task_id": task_id, "provider": provider, "model": computer_model_for(settings, provider)}


@app.get("/ai/computer/{task_id}")
async def ai_computer_status(request: Request, task_id: str):
    _principal(request)
    run = ai_computer.get(task_id)
    if run is None:
        return {"error": "no such computer-control task"}
    return run.public()


@app.get("/ai/computer/{task_id}/trace")
async def ai_computer_trace(request: Request, task_id: str, images: bool = True):
    _principal(request)
    """The whole model-in/model-out trace of one run, for the AI Inspector.

    Answers the four questions that a status line cannot: was the screenshot
    really on the wire, was the computer-control prompt really attached, what
    exactly did the model say, and what exactly did the executor receive.

    `images=false` drops the base64 and keeps every dimension, hash and MIME
    type, which is the shape to use when the trace is being posted somewhere or
    exported -- a dozen full screenshots is several megabytes of text.

    There is no API key, header or token anywhere in this payload: the trace is
    built from the run record, and the wire summary is built from the request
    body with the base64 and headers excluded.
    """
    run = ai_computer.get(task_id)
    if run is None:
        return {"error": "no such computer-control task"}
    # Built with the images and stripped afterwards, so the flag below describes
    # the image that really was there.  Building it without them first would make
    # "was a screenshot withheld?" unanswerable, which is the one question this
    # flag exists to answer.
    report = ai_computer.report(run, include_images=True)
    if not images:
        for turn in report["turns"]:
            turn["image_withheld"] = bool(turn.get("image") or turn.get("next_image"))
            turn.pop("image", None)
            turn.pop("next_image", None)
    return report


@app.post("/ai/computer/{task_id}/stop")
async def ai_computer_stop(request: Request, task_id: str):
    _principal(request)
    if not await ai_computer.stop(task_id):
        return {"error": "no such computer-control task"}
    return {"ok": True, "task_id": task_id}


async def _computer_url() -> str:
    return settings.workspace_base_url


@app.get("/sysinfo")
async def proxy_sysinfo(request: Request):
    _principal(request)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{await _computer_url()}/sysinfo")
            return JSONResponse(content=resp.json())
    except Exception as exc:
        return JSONResponse(content={"ok": False, "error": f"computer unreachable: {exc}"}, status_code=502)


@app.get("/file")
async def proxy_file(
    request: Request,
    path: str = Query("", alias="path"),
    download: bool = False,
):
    """Relay a workspace file, or hand it back untouched as an attachment.

    The same route serves both in-app previews and the Download button, so a
    preview never has to be converted, re-encoded or copied to a second URL
    to be downloadable.  ``download=1`` only adds a Content-Disposition header;
    the bytes are the agent's bytes in both cases.
    """
    _principal(request)
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
async def proxy_cdp_status(request: Request):
    _principal(request)
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


def _authenticated_screen_origin() -> str:
    """The authenticated origin the screen's WebSocket must go through.

    The Quick Tunnel points straight at this process, so a viewer aimed at it
    reaches the screen endpoint with no internal token and is refused -- a 403
    on the upgrade, over and over.  The Cloudflare Worker is the door every
    other call already uses: it owns the session, verifies the browser's
    identity, and injects the internal token the screen endpoint checks.
    `worker_origin` names it; `public_base_url` is the same origin when the app
    is deployed behind it.  This mirrors the routines client, so "the Worker's
    origin" means one thing everywhere.  Empty means no such gateway is
    configured, and the caller falls back to the same-origin bridge.
    """
    return (settings.worker_origin or settings.public_base_url or "").strip().rstrip("/")


@app.get("/screen/config")
async def screen_config(request: Request):
    """Where the Computer view should open its WebSocket.

    The socket is routed through the authenticated Worker origin, never the raw
    Quick Tunnel.  The tunnel bypasses the Worker that injects the internal
    token, so a browser pointed at it has its upgrade refused with a 403; the
    Worker is the same authenticated, cookie-bearing door the rest of the app
    uses, and it forwards the WebSocket upgrade.  With no Worker configured the
    viewer uses the same-origin bridge, which is authenticated the same way.

    The public route is the same origin this app is served from, which is what
    makes the session cookie first-party for the WebSocket upgrade: the browser
    has already proved who it is to load the page, and carries the same cookie
    when it opens the screen.
    """
    _principal(request)
    # Reported for the UI's "the tunnel moved / went stale" note, but it is
    # deliberately not where the socket connects.
    tunnel_origin, age = _published_url()
    origin = _authenticated_screen_origin()
    # Only advertise a public route we can actually vouch for.
    public = f"{origin}/websockify" if origin else ""
    # The WebSocket() constructor would normalise https->wss for us, but handing
    # the viewer a literal wss:// URL keeps the value honest about what it is and
    # keeps it usable by anything that is not a browser.
    ws_public = public.replace("https://", "wss://", 1) if public else ""
    return {
        "ok": True,
        "mode": "tunnel" if public else "bridge",
        # Authenticated by the session cookie at the Worker, which also injects
        # the internal token the backend screen endpoint requires.
        "wsUrl": ws_public,
        # Used only in bridge mode; the client appends nothing to it.
        "bridgePath": "/ws/screen",
        # noVNC negotiates the binary subprotocol; websockify serves it.
        "wsProtocols": ["binary"],
        "websockifyPort": computer.websockify_port,
        # Lets the UI say the tunnel moved, or that it has been gone a while.
        "publicOrigin": tunnel_origin,
        "publicUrlAgeSeconds": round(age, 1),
    }


@app.get("/screen/status")
async def screen_status(request: Request):
    """Real health of the computer's display, for the UI's status line.

    Only a local TCP probe and a status call into the agent: no frame ever
    passes through the backend or the model.
    """
    _principal(request)
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
async def screen_ensure(request: Request):
    _principal(request)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{await _computer_url()}/display/ensure")
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse(content={"ok": False, "error": f"computer unreachable: {exc}"}, status_code=502)


@app.post("/screen/browser/restart")
async def screen_browser_restart(request: Request):
    """Restart the remote browser; the screen then follows the real state."""
    _principal(request)
    result = await computer.restart_browser()
    if not result.get("ok"):
        return JSONResponse(content=result, status_code=502)
    return JSONResponse(content=result)


async def _ws_authenticate(websocket: WebSocket) -> Optional[Principal]:
    """Authenticate a WebSocket the way every HTTP route is authenticated.

    A browser cannot add a header to its own WS handshake, so the trusted
    gateway (the Worker) adds the internal token for the backend to check, and
    either asserts the identity itself (``x-rag-user-sub``, the edge owns the
    session) or the browser proves identity with the first-party session cookie.
    When no internal token is configured (local development) the session alone
    is accepted.  Denied sockets are closed with 1008 so no data is exchanged.
    """
    gateway_ok = False
    if auth.internal_configured:
        upstream = websocket.headers.get(INTERNAL_HEADER, "")
        if not auth.websocket_internal_valid(upstream):
            await websocket.close(code=1008)
            return None
        gateway_ok = True
    if gateway_ok:
        asserted = auth.asserted_principal(websocket.headers)
        if asserted:
            return asserted
    token = websocket.cookies.get(COOKIE_NAME)
    principal = auth.principal_from_token(token)
    if not principal:
        await websocket.close(code=1008)
        return None
    return principal


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
    if await _ws_authenticate(websocket) is None:
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
    if await _ws_authenticate(websocket) is None:
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
    # agents and reads their conversations.  The identity is the session cookie,
    # never the path segment -- a client claiming someone else's user_id in the
    # URL must still prove the matching cookie, and the sender's id is taken
    # from the authenticated session instead.
    principal = await _ws_authenticate(websocket)
    if principal is None:
        return
    owner_id = principal.user_id
    await websocket.accept()
    await websocket.send_json({"type": "connected", "user_id": owner_id})
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
                if not thread_id or not db.thread_owned_by(thread_id, owner_id):
                    thread_id = db.create_thread(owner_id, text[:50])
                    await websocket.send_json({"type": "thread", "thread_id": thread_id})
                active_thread = thread_id
                user_message_id = db.add_message(thread_id, "user", text, agent="user")
                await websocket.send_json({"type": "user_ack", "thread_id": thread_id})
                await run_commander_on_thread(
                    thread_id, owner_id, text, _ws_emit(websocket), user_message_id=user_message_id
                )

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


def _run_error_text(exc: Exception) -> str:
    """The sentence shown in the transcript when a model call fails.

    `ProviderHTTPError` and `ModelCallGateTimeout` both expose a one-line
    `.message` phrased for a person; anything else falls back to `str(exc)` and
    finally to the exception's class name, so a failure is never reported as an
    empty line.
    """
    detail = getattr(exc, "message", "") or str(exc) or exc.__class__.__name__
    return f"The AI request failed: {detail}"


async def _emit_run_error(emit, exc: Exception) -> None:
    """Publish a failed turn as a terminal run event, not a dead socket.

    `emit` is whatever the channel supplied -- the WebSocket bridge (async) or a
    plain list's ``append`` (sync) -- so both are handled, and an emit that
    itself fails is swallowed: the transcript line is the fallback record and
    losing it to a broken socket would be worse than the broken socket.
    """
    if emit is None:
        return
    try:
        result = emit({"type": "error", "error": _run_error_text(exc), "fatal": True})
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


async def _emit_run_done(emit) -> None:
    """Publish the terminal `done` event once the assistant turn is on disk.

    The UI must not tear down its streamed view until the persisted transcript
    actually contains the answer, so `done` is emitted *after* the writes below
    and never before.  Without it the frontend has to guess when the turn ended
    (it used a fixed timer off the `synthesize` event), which raced the write
    and made a finished answer vanish.
    """
    if emit is None:
        return
    try:
        result = emit({"type": "done"})
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


async def run_commander_on_thread(
    thread_id: str, user_id: str, text: str, emit, user_message_id: int = 0
) -> None:
    """Produce one assistant turn from the canonical transcript.

    The transcript is the source of truth: the current user turn is already
    stored (``user_message_id``) and is excluded when assembling context, because
    the agent runner appends the same text itself.  That is the fix for the
    newest question having appeared twice in the model's prompt.

    Tool calls and their results are recorded back into the transcript as they
    happen, so a later turn -- or a summary -- sees the work that was actually
    done rather than only the final answers.
    """
    memory_text = agent_memory.memory_text_for(db, user_id)
    summary_row = db.latest_summary(thread_id)
    summary_text = str((summary_row or {}).get("content") or "")

    rows = db.list_messages(thread_id, tail=200)
    context = assemble_context(
        rows,
        query=text,
        token_budget=settings.history_token_budget,
        exclude_id=user_message_id or None,
        summary_text=summary_text,
        memory_text=memory_text,
    )

    def record(entry: dict) -> None:
        db.add_message(
            thread_id,
            str(entry.get("role") or "tool"),
            str(entry.get("content") or ""),
            agent=str(entry.get("agent") or ""),
            data=entry.get("data") if isinstance(entry.get("data"), dict) else {},
        )

    # Routine tools need the caller's identity, but tool execution has no
    # request.  Bind it for the duration of this turn (and only this turn):
    # owner_sub is the verified Google subject, so a routine can only ever be
    # created or read for the signed-in user.
    user_row = db.get_user(user_id) or {}
    context_token = set_routine_context(
        RoutineContext(
            user_id=user_id,
            google_sub=str(user_row.get("google_sub") or ""),
            agent_id="commander",
            conversation_id=thread_id,
            timezone=str(user_row.get("timezone") or "UTC"),
        )
    )
    try:
        result = await commander.run(text, context, emit, record=record)
    except Exception as exc:
        # A model call that fails must end the turn on purpose.  Letting the
        # exception escape would tear down whichever channel started the run:
        # over the WebSocket the handler would die with no terminal event and
        # the UI would sit on "processing" forever, and over HTTP the route
        # would return a 500 with nothing to show.  The failure is reported as
        # an ordinary run event and the turn ends here.
        await _emit_run_error(emit, exc)
        db.add_message(thread_id, "assistant", _run_error_text(exc), agent="commander")
        db.touch_conversation_activity(
            thread_id, user_id, db.last_message_id(thread_id), settings.summary_idle_seconds
        )
        await _emit_run_done(emit)
        return
    finally:
        reset_routine_context(context_token)
    db.add_message(thread_id, "assistant", result["text"], agent="commander")
    for report in result.get("reports", []):
        title = report.get("title", "")
        role = report.get("role", "")
        db.add_message(thread_id, "assistant", report["report"], agent=f"worker:{role}:{title}")

    # A conversation that just advanced is (re)armed for passive summarisation:
    # the summary only becomes due after the configured quiet period, so an
    # active conversation is never summarised mid-thought.
    db.touch_conversation_activity(
        thread_id, user_id, db.last_message_id(thread_id), settings.summary_idle_seconds
    )
    # Only now -- transcript written -- is the turn safe to close on the client.
    await _emit_run_done(emit)


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


# The deterministic page the computer-control loop is verified against.  Served
# from its own route rather than from the static mount below, because that mount
# only happens when the frontend build is *absent* -- and where the build is
# present, which is the deployment that matters, a page that only exists there is
# a 404 exactly when someone tries to verify a run.
@app.get("/computer-test.html")
async def computer_test_page():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "computer-test.html")
    try:
        with open(path, encoding="utf-8") as handle:
            return Response(content=handle.read(), media_type="text/html; charset=utf-8")
    except OSError:
        return Response(content="computer test page is missing", status_code=500)


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
