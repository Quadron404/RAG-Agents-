"""Gmail connector: the first registered connector.

Sends email only to the authenticated owner's verified Google account email,
through the owner's own connected Gmail account.  The model cannot choose a
recipient, CC, BCC, or From address -- those are resolved server-side.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict

import httpx

from ..auth.deps import AuthService
from ..config import Settings
from ..db import Database
from . import gmail as gmail_connection
from .gmail import GmailError

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class GmailActionError(Exception):
    """A Gmail connector action could not be completed."""


@dataclass
class GmailConnector:
    id: str = "gmail"
    name: str = "Gmail"

    @property
    def actions(self) -> Dict[str, "ConnectorAction"]:
        from ..connectors.registry import ConnectorAction  # noqa: PLC0415

        return {
            "send_email": ConnectorAction(
                id="send_email",
                name="Send email",
                description=(
                    "Send an email to yourself (the signed-in user) through your "
                    "own connected Gmail account. The recipient is fixed to your "
                    "verified Google account email."
                ),
                input_schema={
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
                required_permissions=[GMAIL_SEND_SCOPE],
                data_accessed="none",
                data_changed="sends one outbound email through your Gmail",
                execute=gmail_send_email,
            ),
        }


GMAIL_CONNECTOR = GmailConnector()


async def gmail_send_email(
    *,
    db: Database,
    settings: Settings,
    auth_service: AuthService,
    user_id: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Send an email to the user's own verified address via their Gmail.

    Never accepts a recipient from arguments or the model.  Fails closed if the
    user has no verified email, no connected Gmail with gmail.send, or the Gmail
    API rejects the message.
    """
    content = str(arguments.get("content") or "").strip()
    if not content:
        raise GmailActionError("email content is required")

    user = db.get_user(user_id) or {}
    if not user.get("email_verified"):
        raise GmailActionError("your email address is not verified; cannot send email")
    recipient = str(user.get("email") or "").strip()
    if not recipient:
        raise GmailActionError("you have no email address on file; cannot send email")

    connection = db.get_gmail_connection(user_id)
    if not connection or str(connection.get("status") or "") != "connected":
        raise GmailActionError(
            "no Gmail account is connected; connect your Gmail from Settings and "
            "grant the gmail.send scope before email can be sent"
        )
    scope = str(connection.get("scope") or "")
    if GMAIL_SEND_SCOPE not in scope:
        raise GmailActionError(
            "your connected Gmail does not include sending permission; reconnect "
            "with the gmail.send scope"
        )

    connected_email = str(connection.get("email") or "").strip()
    if connected_email and connected_email.lower() != recipient.lower():
        raise GmailActionError(
            "the connected Gmail account does not match your verified account; "
            "cannot send"
        )

    try:
        access = await gmail_connection.access_token(db, auth_service, settings, user_id)
    except GmailError as exc:
        raise GmailActionError(f"could not prepare your Gmail account: {exc}")

    sender_email = connected_email or recipient
    subject = "RAG Agents"
    mime = _build_mime(sender_email, recipient, subject, content)
    raw = (
        base64.urlsafe_b64encode(mime.encode("utf-8")).decode("utf-8").replace("=", "")
    )
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                GMAIL_SEND_URL, headers=headers, content=json.dumps({"raw": raw})
            )
            if resp.status_code >= 500:
                raise GmailActionError(f"Gmail API error {resp.status_code}; the email was not sent")
            if resp.status_code >= 400:
                raise GmailActionError(f"Gmail API rejected the message {resp.status_code}; the email was not sent")
            try:
                data = resp.json()
            except Exception:
                data = {}
    except (httpx.HTTPError, OSError) as exc:
        raise GmailActionError(f"could not reach the Gmail API: {type(exc).__name__}; the email was not sent")

    message_id = str(data.get("id") or "")
    if not message_id:
        raise GmailActionError("the Gmail API accepted the request but returned no message id")

    import uuid as _uuid
    from ..routines.context import get_current

    ctx = get_current()
    agent_id = str(getattr(ctx, "agent_id", "") or "") if ctx else ""
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
        idempotency_key=f"gmail-connector:{_uuid.uuid4().hex}",
    )
    return {
        "ok": True,
        "connector": "gmail",
        "action": "send_email",
        "recipient": recipient,
        "subject": subject,
        "message_id": message_id,
        "note": "accepted by Gmail; inbox delivery is not guaranteed",
    }


def _build_mime(sender: str, to: str, subject: str, body: str) -> str:
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg.as_string()
