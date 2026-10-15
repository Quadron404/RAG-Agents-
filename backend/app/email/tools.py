"""Owner-only notification tools: draft / update / send for owner notifications.

The backend owns the recipient and the sender.  The model can only ever set a
subject and a body; the recipient is resolved from the authenticated owner's
verified identity and the sender is the configured bot-owned mailbox.  A tool
call cannot name a recipient, change the sender, add a Cc/Bcc, or reach any other
account -- those fields are not in the schema, and a call that tries to smuggle
one in is rejected outright.

Ownership is taken from the routine/agent identity ContextVar, which is bound to
the verified session for exactly one user turn.  When it is absent -- during a
routine run, a summary, or any path with no signed-in owner -- the tools fail
closed rather than guessing who the owner is.

The same rules apply no matter who is asking: a prompt injection in a web page,
a fetched document or another agent's output cannot move the recipient, because
the recipient never comes from anything the model produced.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..config import Settings
from ..db import Database
from ..routines.context import get_current
from ..tools.base import Tool
from .sender import BotSender, BotSenderError

#: The tool names this module owns.  ``tools/executor.py`` routes exactly these
#: here and nothing else, so a stray name cannot reach a mailbox by accident.
NOTIFICATION_TOOL_NAMES = {
    "draft_owner_notification",
    "update_owner_notification",
    "send_owner_notification",
}

#: A subject longer than this is almost certainly a mistake, not a message.
SUBJECT_MAX_CHARS = 300
#: The body is capped well below anything that could be mistaken for a bulk
#: mailing; a notification is a note, not a newsletter.
BODY_MAX_CHARS = 100_000
#: Per-owner ceiling on how many notifications may actually be delivered in a
#: rolling hour.  This is the rate limit that keeps a loop or a misbehaving model
#: from turning the bot account into a spam source.
MAX_SENDS_PER_HOUR = 20

#: Field names that would let a caller redirect the message or forge headers.
#: Their presence in a tool call is a hard refusal, whoever sent it.
FORBIDDEN_ARG_FIELDS = frozenset(
    {
        "to",
        "cc",
        "bcc",
        "from",
        "from_",
        "reply_to",
        "replyto",
        "reply-to",
        "recipient",
        "recipient_email",
        "email",
        "address",
        "to_alias",
        "sender",
        "sender_email",
        "smtp",
        "smtp_host",
        "headers",
    }
)


def notification_tools() -> List[Tool]:
    """The three owner-notification tools.

    The schemas are the public contract: there is no recipient, sender, Cc, Bcc
    or header field anywhere, so a conforming caller cannot even express a
    destination -- the destination is not an input.
    """
    return [
        Tool(
            "draft_owner_notification",
            "Create a draft notification to the account owner (the signed-in user). "
            "You choose only the subject and body; the recipient and sender are fixed "
            "by the system and cannot be set. Use this to prepare a message, then "
            "send_owner_notification to deliver it.",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "short subject line"},
                    "body": {"type": "string", "description": "the message body, plain text"},
                },
                "required": ["subject", "body"],
            },
        ),
        Tool(
            "update_owner_notification",
            "Change the subject and/or body of a notification draft you created. "
            "You can only edit your own draft; the recipient and sender cannot be changed.",
            {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["draft_id"],
            },
        ),
        Tool(
            "send_owner_notification",
            "Send a notification draft to the account owner using the bot's own "
            "mailbox. The recipient is the owner and the sender is the bot account; "
            "neither can be changed and no other address can be reached. You may only "
            "send a draft you created, and a draft can be sent only once.",
            {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                },
                "required": ["draft_id"],
            },
        ),
    ]


def _result(output: str = "", error: str = "") -> Dict[str, object]:
    return {"type": "result", "output": output, "error": error}


def _reject_foreign_fields(args: Dict[str, object]) -> Optional[Dict[str, object]]:
    """Refuse a call that tries to set a recipient or a header.

    The schemas already omit these fields, so a well-behaved model never sends
    them; their presence means something is trying to redirect the message.  It
    is treated as a refusal rather than ignored, because silent ignoring would
    let a caller believe it had chosen a recipient.
    """
    for key in args:
        if str(key).strip().lower() in FORBIDDEN_ARG_FIELDS:
            return _result(
                error=(
                    f"the field '{key}' is not allowed: the recipient and sender are "
                    "fixed by the system and cannot be set by a tool call"
                )
            )
    return None


def _identity_or_error() -> tuple[Optional[object], Optional[Dict[str, object]]]:
    ctx = get_current()
    if ctx is None:
        return None, _result(
            error="owner notifications are unavailable: no authenticated owner is bound to this turn"
        )
    return ctx, None


async def run_notification_tool(
    name: str, args: Dict[str, object], settings: Settings, db: Database
) -> Dict[str, object]:
    """Execute one notification tool and return a single ``result`` event."""
    foreign = _reject_foreign_fields(args)
    if foreign is not None:
        return foreign

    ctx, err = _identity_or_error()
    if err is not None:
        return err
    user_id = str(ctx.user_id)  # type: ignore[attr-defined]
    agent_id = str(ctx.agent_id)  # type: ignore[attr-defined]
    conversation_id = str(ctx.conversation_id)  # type: ignore[attr-defined]

    sender = BotSender(settings)

    if name == "draft_owner_notification":
        subject = str(args.get("subject") or "")
        body = str(args.get("body") or "")
        if not subject.strip() or not body.strip():
            return _result(error="both a subject and a body are required")
        if len(subject) > SUBJECT_MAX_CHARS:
            return _result(error=f"subject is too long (limit {SUBJECT_MAX_CHARS} characters)")
        if len(body) > BODY_MAX_CHARS:
            return _result(error=f"body is too long (limit {BODY_MAX_CHARS} characters)")
        draft_id = db.create_email_draft(
            user_id=user_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            subject=subject,
            body=body,
        )
        return _result(output=f"draft created: {draft_id}")

    if name == "update_owner_notification":
        draft_id = str(args.get("draft_id") or "").strip()
        subject = str(args.get("subject") or "")
        body = str(args.get("body") or "")
        if not draft_id:
            return _result(error="draft_id is required")
        if len(subject) > SUBJECT_MAX_CHARS:
            return _result(error=f"subject is too long (limit {SUBJECT_MAX_CHARS} characters)")
        if len(body) > BODY_MAX_CHARS:
            return _result(error=f"body is too long (limit {BODY_MAX_CHARS} characters)")
        draft = db.get_email_draft(draft_id)
        if not draft or str(draft.get("user_id")) != user_id:
            # The same message for "does not exist" and "belongs to someone
            # else": a caller must not be able to probe for other owners' ids.
            return _result(error="draft not found")
        if str(draft.get("status")) != "draft":
            return _result(error="this notification has already been sent and can no longer be edited")
        # Keep whichever part the caller did not supply.
        new_subject = subject if "subject" in args else str(draft.get("subject") or "")
        new_body = body if "body" in args else str(draft.get("body") or "")
        if not new_subject.strip() or not new_body.strip():
            return _result(error="both a subject and a body are required")
        ok = db.update_email_draft(draft_id=draft_id, user_id=user_id, subject=new_subject, body=new_body)
        if not ok:
            return _result(error="draft not found")
        return _result(output="draft updated")

    if name == "send_owner_notification":
        return await _send_owner_notification(args, db, sender, user_id, agent_id, conversation_id)

    return _result(error=f"unknown notification tool {name}")


async def _send_owner_notification(
    args: Dict[str, object],
    db: Database,
    sender: BotSender,
    user_id: str,
    agent_id: str,
    conversation_id: str,
) -> Dict[str, object]:
    draft_id = str(args.get("draft_id") or "").strip()
    if not draft_id:
        return _result(error="draft_id is required")

    draft = db.get_email_draft(draft_id)
    if not draft or str(draft.get("user_id")) != user_id:
        return _result(error="draft not found")

    status = str(draft.get("status"))
    if status == "sent":
        existing = db.get_email_send_by_idem(f"owner-notification:{draft_id}")
        return _result(output=f"already sent: {str((existing or {}).get('email_id') or draft_id)}")
    if status == "sending":
        return _result(error="this notification is already being sent")
    if status != "draft":
        return _result(error="this notification is not available to send")

    # The recipient is the authenticated owner's verified email, read from the
    # user row the session resolved to -- never from the tool call, the message
    # text or the model.
    user = db.get_user(user_id) or {}
    recipient = str(user.get("email") or "").strip()
    if not user.get("email_verified"):
        return _result(error="the owner's email address is not verified; cannot send notification")
    if not recipient:
        return _result(error="the owner has no email address on file; cannot send notification")

    # Fail closed on an unconfigured sender: never fall back to the owner's own
    # connected Gmail or any other path.
    if not sender.configured():
        return _result(error="owner notifications are not configured on this server")

    if _over_rate_limit(db, user_id):
        return _result(error=f"notification rate limit reached (max {MAX_SENDS_PER_HOUR} per hour)")

    # Atomic claim: only one concurrent attempt can move the draft out of
    # ``draft`` and reach the provider, so a message is never delivered twice.
    if not db.claim_email_draft_for_send(draft_id=draft_id, user_id=user_id):
        current = db.get_email_draft(draft_id) or {}
        if str(current.get("status")) == "sent":
            existing = db.get_email_send_by_idem(f"owner-notification:{draft_id}")
            return _result(output=f"already sent: {str((existing or {}).get('email_id') or draft_id)}")
        return _result(error="this notification is already being sent")

    described = sender.describe()
    provider = str(described.get("provider") or "")
    sender_email = str(described.get("sender_email") or "")

    try:
        provider_message_id = await sender.send(
            recipient, str(draft.get("subject") or ""), str(draft.get("body") or "")
        )
    except BotSenderError as exc:
        # Release the claim so the owner's next turn can retry, and record the
        # attempt without any secret or message body.
        db.set_email_draft_status(draft_id=draft_id, user_id=user_id, status="draft")
        db.create_email_audit(
            user_id=user_id,
            agent_id=agent_id,
            draft_id=draft_id,
            recipient_email=recipient,
            sender_email=sender_email,
            provider=provider,
            status="failed",
            error_code=getattr(exc, "error_code", "") or "sender_error",
        )
        return _result(error=f"could not send notification: {exc}")

    # Only now, with the provider's confirmation in hand, is the draft marked
    # sent and the send recorded.  A claim of "sent" is never made before this.
    db.mark_email_draft_sent(draft_id)
    db.create_email_send(
        user_id=user_id,
        agent_id=agent_id,
        draft_id=draft_id,
        subject=str(draft.get("subject") or ""),
        recipient_email=recipient,
        sender_email=sender_email,
        provider_message_id=provider_message_id or "",
        status="sent",
    )
    db.create_email_audit(
        user_id=user_id,
        agent_id=agent_id,
        draft_id=draft_id,
        recipient_email=recipient,
        sender_email=sender_email,
        provider=provider,
        status="sent",
        provider_message_id=provider_message_id or "",
    )
    return _result(output="notification sent to the owner")


def _over_rate_limit(db: Database, user_id: str) -> bool:
    """True when this owner has already hit the hourly notification ceiling."""
    import time as _time

    cutoff = _time.time() - 3600.0
    recent = 0
    for row in db.list_email_sends(user_id, tail=MAX_SENDS_PER_HOUR + 5):
        try:
            if float(row.get("created_at") or 0) >= cutoff:
                recent += 1
        except (TypeError, ValueError):
            continue
    return recent >= MAX_SENDS_PER_HOUR
