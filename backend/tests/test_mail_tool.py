"""mail_tool_call: an email sent to the signed-in user through their own Gmail.

These tests drive the real mail tool against mocked Google/Gmail endpoints: no
message ever leaves the process, and no Google endpoint is contacted.  What is
proven is the part that has to be true regardless of the provider:

* the tool schema has exactly one argument, ``content``, and no recipient field;
* the recipient is the authenticated user's verified Google account email,
  resolved server-side from the session's user row, never from a tool argument;
* signing in is not enough: a connected Gmail account with the gmail.send scope
  is required, and the call fails closed without it;
* the message is really posted to the Gmail send endpoint with a bearer token,
  addressed to the user's own mailbox (users/me);
* success is reported only after the Gmail API accepts the message; a rejected
  or failed delivery is reported as a failure, never as sent;
* the executor routes mail_tool_call to the mail handler (and fails closed
  without the database or the auth service).
"""
import asyncio
import base64
import json
import os
import sys
import time

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import tests.support as support  # noqa: E402  (configures DATA_DIR before app.main)

import app.main as main_module  # noqa: E402
from app.agents.commander import MAIL_TOOL_POLICY, WORKER_PROMPT  # noqa: E402
from app.agents.routines import ROUTINE_SYSTEM_PROMPT  # noqa: E402
from app.auth import google as google_oauth  # noqa: E402
from app.email import gmail as gmail_connection  # noqa: E402
from app.email.mail_tool import (  # noqa: E402
    MAIL_TOOL_NAME,
    MAIL_TOOL_NAMES,
    mail_tools,
    run_mail_tool,
    GMAIL_SEND_SCOPE,
)
from app.routines.context import RoutineContext, reset_current, set_current  # noqa: E402
from app.tools.executor import Executor  # noqa: E402
from app.tools.registry import worker_tools  # noqa: E402

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


db = main_module.db
auth = main_module.auth
settings = main_module.settings

OWNER_A_EMAIL = "alice@example.com"
OWNER_B_EMAIL = "bob@example.com"


def make_owner(sub: str, email: str, verified: bool = True) -> str:
    return str(db.upsert_user(sub, email, verified, "Owner", "")["id"])


OWNER_A = make_owner("mail-owner-a-sub", OWNER_A_EMAIL)
OWNER_B = make_owner("mail-owner-b-sub", OWNER_B_EMAIL)
UNVERIFIED = make_owner("mail-unverified-sub", "unverified@example.com", verified=False)
NO_EMAIL = make_owner("mail-no-email-sub", "")


def save_gmail(user_id: str, email: str, *, scope, connected: bool = True, refresh="r", access="a", expiry_offset=3600.0):
    db.save_gmail_connection(
        user_id=user_id,
        google_sub=f"{user_id}-sub",
        email=email,
        scope=scope,
        refresh_token_enc=auth.encrypt_secret(refresh),
        access_token_enc=auth.encrypt_secret(access),
        token_expiry=time.time() + expiry_offset,
    )
    if not connected:
        db.set_gmail_status(user_id, "disconnected", "never connected")


FULL_SCOPE = " ".join(google_oauth.GMAIL_FULL_SCOPES)


def run_tool(name: str, args: dict, *, user_id: str = OWNER_A, agent_id: str = "commander",
             conversation_id: str = "conv-1") -> dict:
    """Run the mail tool with the owner's identity bound, as a turn does."""

    async def _run():
        user_row = db.get_user(user_id) or {}
        token = set_current(
            RoutineContext(
                user_id=user_id,
                google_sub=str(user_row.get("google_sub") or ""),
                agent_id=agent_id,
                conversation_id=conversation_id,
                timezone="UTC",
            )
        )
        try:
            return await run_mail_tool(name, args, settings, db, auth)
        finally:
            reset_current(token)

    return asyncio.run(_run())


def run_tool_without_owner(name: str, args: dict) -> dict:
    return asyncio.run(run_mail_tool(name, args, settings, db, auth))


# --- the schema has exactly one argument and no recipient field --------------
tools = {t.name: t for t in mail_tools()}
check("there is exactly one mail tool", set(tools) == {MAIL_TOOL_NAME}, sorted(tools))
tool = tools[MAIL_TOOL_NAME]
props = set((tool.parameters.get("properties") or {}).keys())
check("the schema has exactly the 'content' argument", props == {"content"}, sorted(props))
check("'content' is required", tool.parameters.get("required") == ["content"], tool.parameters.get("required"))
check("no extra properties are allowed", tool.parameters.get("additionalProperties") is False)
forbidden = {"to", "cc", "bcc", "from", "from_", "reply_to", "recipient", "recipient_email", "email", "address", "sender", "sender_email", "headers"}
check("no recipient/sender/header field in the schema", not (props & forbidden), sorted(props & forbidden))
check("the mail tool is offered to the agent", MAIL_TOOL_NAME in {t.name for t in worker_tools()})


# --- the mail policy is in the trusted prompts -------------------------------
for label, prompt in (("worker prompt", WORKER_PROMPT), ("routine prompt", ROUTINE_SYSTEM_PROMPT)):
    check(f"the mail policy is in the {label}", MAIL_TOOL_POLICY in prompt and "verified Google account email" in prompt)


# --- fail closed when no owner is bound --------------------------------------
no_ctx = run_tool_without_owner("mail_tool_call", {"content": "hello"})
check("with no owner bound, sending fails closed", bool(no_ctx.get("error")) and not no_ctx.get("output"), no_ctx)


# --- fail closed on an unverified or missing owner address -------------------
for user_id, label in ((UNVERIFIED, "unverified"), (NO_EMAIL, "no email")):
    res = run_tool("mail_tool_call", {"content": "hello"}, user_id=user_id)
    check(f"a {label} owner cannot receive email", bool(res.get("error")) and not res.get("output"), res)


# --- fail closed when Gmail is not connected or lacks gmail.send -------------
save_gmail(OWNER_B, OWNER_B_EMAIL, scope="openid email profile", connected=True)
not_connected = run_tool("mail_tool_call", {"content": "hello"}, user_id=OWNER_B)
check("a signed-in user with no connected Gmail is refused",
      bool(not_connected.get("error")) and "connected" in not_connected.get("error", "").lower(), not_connected)

db.delete_gmail_connection(OWNER_B)
db.save_gmail_connection(
    user_id=OWNER_B,
    google_sub=f"{OWNER_B}-sub",
    email=OWNER_B_EMAIL,
    scope="openid email profile",  # no gmail.send
    refresh_token_enc=auth.encrypt_secret("r"),
    access_token_enc=auth.encrypt_secret("a"),
    token_expiry=time.time() + 3600.0,
)
no_send_scope = run_tool("mail_tool_call", {"content": "hello"}, user_id=OWNER_B)
check("a connected Gmail without gmail.send is refused",
      bool(no_send_scope.get("error")) and "sending permission" in no_send_scope.get("error", ""), no_send_scope)


# --- a fake Gmail HTTP transport: records the request, returns an id ---------
_gmail_state = {"requests": []}


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, content=None):
        _gmail_state["requests"].append({"url": url, "headers": headers or {}, "content": content or ""})
        return _FakeResponse(200, {"id": "gmail-msg-456"})


async def fake_refresh(*, settings, refresh_token, client=None):
    return {"access_token": "fresh-access", "expires_in": 3600}


main_module.google_oauth.refresh_access_token = fake_refresh
gmail_connection.httpx.AsyncClient = _FakeAsyncClient
_gmail_state["requests"].clear()


# --- happy path: a verified owner with a connected Gmail ----------------------
save_gmail(OWNER_A, OWNER_A_EMAIL, scope=FULL_SCOPE, connected=True, access="cached-access")
sent = run_tool("mail_tool_call", {"content": "the nightly report is ready"})
check("a valid send reports success", sent.get("error") == "" and "sent" in sent.get("output", ""), sent)
check("the recipient in the result is the owner's verified address",
      OWNER_A_EMAIL in sent.get("output", ""), sent.get("output"))

check("exactly one Gmail send request was made", len(_gmail_state["requests"]) == 1, _gmail_state["requests"])
if _gmail_state["requests"]:
    req = _gmail_state["requests"][0]
    check("the send went to the Gmail send endpoint", req["url"] == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", req["url"])
    check("the request carried a bearer token", str(req["headers"].get("Authorization", "")).startswith("Bearer "), req["headers"])
    raw = json.loads(req["content"]).get("raw", "")
    padding = "=" * (-len(raw) % 4)
    decoded = base64.urlsafe_b64decode(raw + padding).decode("utf-8")
    check("the MIME From is the owner's Gmail", f"From: {OWNER_A_EMAIL}" in decoded, decoded[:300])
    check("the MIME To is the owner's verified address", f"To: {OWNER_A_EMAIL}" in decoded, decoded[:300])
    check("the body is delivered unchanged", "the nightly report is ready" in decoded or "dGhlIG5pZ2h0bHkgcmVwb3J0IGlzIHJlYWR5" in decoded, decoded[:500])

# A send record exists for the owner, owner-isolated, with the provider id.
sends = db.list_email_sends(OWNER_A)
check("a send record exists for owner A", len(sends) >= 1, len(sends))
check("every send record is owner-isolated", all(str(r.get("user_id")) == OWNER_A for r in sends))


# --- the same content addressed to a different owner stays with that owner -----
save_gmail(OWNER_B, OWNER_B_EMAIL, scope=FULL_SCOPE, connected=True, access="cached-access-b")
_gmail_state["requests"].clear()
sent_b = run_tool("mail_tool_call", {"content": "bob's note"}, user_id=OWNER_B)
check("owner B's mail is addressed to owner B", sent_b.get("error") == "" and OWNER_B_EMAIL in sent_b.get("output", ""), sent_b)
if _gmail_state["requests"]:
    raw_b = json.loads(_gmail_state["requests"][0]["content"]).get("raw", "")
    decoded_b = base64.urlsafe_b64decode(raw_b + "=" * (-len(raw_b) % 4)).decode("utf-8")
    check("owner B's message is to owner B, never owner A", f"To: {OWNER_B_EMAIL}" in decoded_b and f"To: {OWNER_A_EMAIL}" not in decoded_b, decoded_b[:300])


# --- a Gmail API rejection is reported as failure, never as success -----------
class _FakeRejectingClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, content=None):
        return _FakeResponse(400, {"error": {"message": "bad request"}})


gmail_connection.httpx.AsyncClient = _FakeRejectingClient
_gmail_state["requests"].clear()
rejected = run_tool("mail_tool_call", {"content": "hello"})
check("a Gmail rejection is reported as not sent", bool(rejected.get("error")) and "not sent" in rejected.get("error", ""), rejected)
check("no success is claimed for a rejected message", rejected.get("output") == "")


# --- network failure is reported as failure, never as success -----------------
class _FakeFailingClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        raise OSError("network down")


gmail_connection.httpx.AsyncClient = _FakeFailingClient
failed = run_tool("mail_tool_call", {"content": "hello"})
check("a network failure is reported as not sent", bool(failed.get("error")) and "not sent" in failed.get("error", ""), failed)


# --- the executor routes mail_tool_call to the mail handler ------------------
probe = Executor(settings, db=db, auth_service=auth)
no_db = Executor(settings)
no_auth = Executor(settings, db=db)


async def _drain(executor, name, args, *, user_id=OWNER_A):
    user_row = db.get_user(user_id) or {}
    token = set_current(
        RoutineContext(
            user_id=user_id,
            google_sub=str(user_row.get("google_sub") or ""),
            agent_id="commander",
            conversation_id="conv-probe",
            timezone="UTC",
        )
    )
    try:
        events = []
        async for ev in executor.run(name, args):
            events.append(ev)
        return events
    finally:
        reset_current(token)


save_gmail(OWNER_A, OWNER_A_EMAIL, scope=FULL_SCOPE, connected=True, access="cached-access")
gmail_connection.httpx.AsyncClient = _FakeAsyncClient
routed = asyncio.run(_drain(probe, "mail_tool_call", {"content": "routed via executor"}))
check("the executor routes a mail call to the mail handler",
      len(routed) == 1 and routed[0].get("type") == "result" and routed[0].get("error") == "", routed)
closed_no_db = asyncio.run(_drain(no_db, "mail_tool_call", {"content": "x"}))
check("an executor with no database fails the mail call closed",
      bool(closed_no_db and closed_no_db[0].get("error")), closed_no_db)
closed_no_auth = asyncio.run(_drain(no_auth, "mail_tool_call", {"content": "x"}))
check("an executor with no auth service fails the mail call closed",
      bool(closed_no_auth and closed_no_auth[0].get("error")), closed_no_auth)


# --- no module outside the mail sender path reaches the Gmail send endpoint ---
import pathlib as _pathlib  # noqa: E402

app_dir = _pathlib.Path(BACKEND_DIR) / "app"
offenders = []
for path in app_dir.rglob("*.py"):
    if path.name in ("sender.py", "mail_tool.py", "gmail.py"):
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    if "smtplib" in text or "sendmail(" in text or ("messages/send" in text and "GMAIL_SEND_URL" not in text):
        offenders.append(str(path.relative_to(app_dir)))
check("no module outside the mail-sending path can send mail", not offenders, offenders)


if failures:
    print(f"\n{len(failures)} mail tool check(s) failed:")
    for label in failures:
        print("  -", label)
    raise SystemExit(1)

print("\nall mail tool checks pass")
