"""mail_tool_call: send an email to the signed-in user through their own Gmail.

The recipient is the authenticated user's verified Google account email, resolved
server-side from the session's user row. It is never an input: the tool takes only
``content``, and the model cannot name a recipient, a sender, a Cc or a Bcc.

Mail is sent through the user's own connected Gmail account (the ``gmail.send``
grant obtained by a separate, explicit consent), addressed to ``users/me`` -- never
to any address the model, the frontend, the tool arguments or the message content
names.  Signing in with Google alone does not grant this; the user must have
connected a mailbox with the gmail.send scope, and the call fails closed otherwise.

Nothing about the send -- no token, no header, no recipient choice -- reaches the
browser, a model prompt or a log line.
"""
from __future__ import annotations

import base64
import json
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

import httpx

from ..auth.deps import AuthService
from ..config import Settings
from ..db import Database
from . import gmail as gmail_connection
from .gmail import GmailError
from ..routines.context import get_current
from ..tools.base import Tool
from .sender import GMAIL_SEND_URL

#: The Gmail scope that must be present on the connected mailbox before mail can
#: be sent.  Google sign-in (openid email profile) does not include it.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

MAIL_TOOL_NAME = "mail_tool_call"
MAIL_TOOL_NAMES = frozenset({MAIL_TOOL_NAME})


def mail_tools() -> List[Tool]:
    """The single mail tool.

    The schema has exactly one argument, ``content``.  There is no recipient,
    sender, Cc, Bcc, From or Reply-To field, so a conforming caller cannot even
    express a destination -- the destination is resolved server-side from the
    verified session and is not an input.
    """
    return [
        Tool(
            MAIL_TOOL_NAME,
            "Send an email to yourself (the signed-in user). The recipient is fixed "
            "to your verified Google account email and cannot be set or changed; the "
            "sender is your own connected Gmail account. Provide only the message "
            "content.",
            {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The email content to send to the user.",
                    }
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        )
    ]


def _result(output: str = "", error: str = "") -> Dict[str, object]:
    return {"type": "result", "output": output, "error": error}


def _build_mime(sender: str, to: str, subject: str, body: str) -> str:
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg.as_string()


async def run_mail_tool(
    name: str,
    args: Dict[str, object],
    settings: Settings,
    db: Database,
    auth_service: AuthService,
) -> Dict[str, object]:
    """Execute ``mail_tool_call`` and return a single ``result`` event.

    Never accepts a recipient from the call, never sends to anyone but the
    authenticated user, and reports success only after the Gmail API accepts the
    message.
    """
    content = str(args.get("content") or "").strip()
    if not content:
        return _result(error="email content is required")

    # The authenticated user comes from the bound turn context.  It never comes
    # from the tool call.
    ctx = get_current()
    if ctx is None:
        return _result(
            error="email is unavailable: no authenticated user is bound to this turn"
        )
    user_id = str(ctx.user_id)

    user = db.get_user(user_id) or {}
    if not user.get("email_verified"):
        return _result(
            error="your email address is not verified; cannot send email"
        )
    recipient = str(user.get("email") or "").strip()
    if not recipient:
        return _result(
            error="you have no email address on file; cannot send email"
        )

    # A connected Gmail account with the gmail.send scope is required.  Sign-in
    # alone (openid email profile) does not grant sending permission.
    connection = db.get_gmail_connection(user_id)
    if not connection or str(connection.get("status") or "") != "connected":
        return _result(
            error=(
                "no Gmail account is connected; connect your Gmail from Settings and "
                "grant the gmail.send scope before email can be sent"
            )
        )
    scope = str(connection.get("scope") or "")
    if GMAIL_SEND_SCOPE not in scope:
        return _result(
            error=(
                "your connected Gmail does not include sending permission; reconnect "
                "with the gmail.send scope"
            )
        )

    # A fresh access token for the user's own mailbox, refreshed server-side from
    # the stored refresh token when needed.  Tokens are encrypted at rest and never
    # leave this function.
    try:
        access = await gmail_connection.access_token(
            db, auth_service, settings, user_id
        )
    except GmailError as exc:
        return _result(
            error=f"could not prepare your Gmail account: {exc}"
        )

    sender_email = str(connection.get("email") or recipient).strip()
    if not sender_email:
        sender_email = recipient

    # Matching identity check: the connected Gmail must be the same account.
    if sender_email.lower() != recipient.lower():
        return _result(
            error=(
                "the connected Gmail account does not match your verified account; "
                "cannot send"
            )
        )

    subject = "RAG Agents"
    mime = _build_mime(sender_email, recipient, subject, content)
    raw = (
        base64.urlsafe_b64encode(mime.encode("utf-8"))
        .decode("utf-8")
        .replace("=", "")
    )
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                GMAIL_SEND_URL, headers=headers, content=json.dumps({"raw": raw})
            )
            if resp.status_code >= 500:
                return _result(
                    error=f"Gmail API error {resp.status_code}; the email was not sent"
                )
            if resp.status_code >= 400:
                return _result(
                    error=f"Gmail API rejected the message {resp.status_code}; "
                    "the email was not sent"
                )
            try:
                data = resp.json()
            except Exception:
                data = {}
    except (httpx.HTTPError, OSError) as exc:
        return _result(
            error=f"could not reach the Gmail API: {type(exc).__name__}; "
            "the email was not sent"
        )

    message_id = str(data.get("id") or "")
    if not message_id:
        return _result(
            error="the Gmail API accepted the request but returned no message id; "
            "the email was not confirmed sent"
        )

    # Record the durable send (owner-isolated) so the outcome is auditable.
    agent_id = str(getattr(ctx, "agent_id", "") or "")
    conversation_id = str(getattr(ctx, "conversation_id", "") or "")
    db.create_email_send(
        user_id=user_id,
        agent_id=agent_id,
        draft_id="",
        subject=subject,
        recipient_email=recipient,
        sender_email=sender_email,
        provider_message_id=message_id,
        gmail_message_id=message_id,
        status="sent",
        idempotency_key=f"mail-tool-call:{uuid.uuid4().hex}",
    )
    return _result(output=f"email sent to {recipient}")
