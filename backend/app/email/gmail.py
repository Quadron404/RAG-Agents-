"""The Gmail connection lifecycle: the one place an access token is minted.

A Gmail connection stores a long-lived refresh token (encrypted) and a short-lived
access token (encrypted).  Every later Gmail call needs a *fresh* access token,
so this module owns the three things that must be true for that to be safe:

* **Refresh, not retry-forever.**  An access token past its expiry is refreshed
  with the stored refresh token.  A refresh that fails because the grant is gone
  (``invalid_grant``) is *permanent*: the connection is marked ``revoked`` and
  the caller is told to reconnect, and no further refresh is attempted for it.
  A transient failure (a 5xx, a network error) is *retryable*: the connection is
  marked ``error`` and the next call tries again.
* **Nothing leaks.**  The refresh token and access token never appear in a return
  value, an error, a status payload or a log line; only the mailbox address and a
  coarse status are ever reported.
* **Per-user.**  Every read and write is keyed by ``user_id``; a caller cannot
  reach another user's connection because there is no code path that does not
  take the owner's id.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from ..auth import google as google_oauth

#: Statuses that mean the stored refresh token cannot be used again and the user
#: must re-run the consent flow.  Once a connection is in one of these, the
#: refresh path refuses *before* calling Google, which is what stops a revoked
#: authorization from being retried indefinitely.
NEEDS_RECONNECT_STATUSES = frozenset({"revoked", "reauth_required"})


class GmailError(RuntimeError):
    """A Gmail operation failed, with the actionable facts attached.

    ``retryable`` says whether trying again could plausibly work now; when it is
    False the fix is a reconnect, and ``needs_reconnect`` is True.  The message is
    safe to surface -- it never contains a token.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str = "error",
        retryable: bool = True,
        needs_reconnect: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = bool(retryable)
        self.needs_reconnect = bool(needs_reconnect)


def connection_summary(connection: Optional[dict]) -> dict:
    """A safe, frontend-shaped view of a connection row.

    Deliberately narrow: the mailbox address and the state, never a token or the
    encrypted columns.  ``connected`` is true only for a healthy connection, so a
    revoked or errored mailbox is never reported as connected.
    """
    if not connection:
        return {"connected": False, "email": "", "status": "disconnected", "needsReconnect": False}
    status = str(connection.get("status") or "disconnected")
    return {
        "connected": status == "connected",
        "email": str(connection.get("email") or ""),
        "status": status,
        "needsReconnect": status in NEEDS_RECONNECT_STATUSES,
    }


def status_for(db, user_id: str) -> dict:
    return connection_summary(db.get_gmail_connection(user_id))


async def access_token(
    db,
    auth_service,
    settings,
    user_id: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Return a usable access token for the user's mailbox, refreshing if needed.

    Raises :class:`GmailError` on any failure, carrying whether the caller should
    retry or ask the user to reconnect.
    """
    connection = db.get_gmail_connection(user_id)
    if not connection:
        raise GmailError(
            "no Gmail account is connected",
            status="disconnected",
            retryable=False,
            needs_reconnect=True,
        )

    status = str(connection.get("status") or "")
    if status in NEEDS_RECONNECT_STATUSES:
        raise GmailError(
            "Gmail authorization is no longer valid; reconnect required",
            status=status,
            retryable=False,
            needs_reconnect=True,
        )

    # A cached access token that is not about to expire is used as-is.
    expiry = float(connection.get("token_expiry") or 0)
    skew = float(getattr(settings, "gmail_refresh_skew_seconds", 60))
    if connection.get("access_token_enc") and expiry > time.time() + skew:
        cached = auth_service.decrypt_secret(str(connection.get("access_token_enc") or ""))
        if cached:
            return cached

    refresh = auth_service.decrypt_secret(str(connection.get("refresh_token_enc") or ""))
    if not refresh:
        # The stored value cannot be read (e.g. APP_ENCRYPTION_KEY was rotated).
        # That is permanent until the user reconnects, so say so rather than loop.
        db.set_gmail_status(user_id, "reauth_required", "stored authorization could not be read")
        raise GmailError(
            "stored Gmail authorization could not be read; reconnect required",
            status="reauth_required",
            retryable=False,
            needs_reconnect=True,
        )

    try:
        payload = await google_oauth.refresh_access_token(
            settings=settings, refresh_token=refresh, client=client
        )
    except google_oauth.OAuthError as exc:
        code = str(getattr(exc, "error_code", "") or "")
        if not getattr(exc, "retryable", True) or code in google_oauth.PERMANENT_ERROR_CODES:
            db.set_gmail_status(user_id, "revoked", f"refresh rejected ({code or 'permanent'})")
            raise GmailError(
                "Gmail authorization is no longer valid; reconnect required",
                status="revoked",
                retryable=False,
                needs_reconnect=True,
            ) from None
        db.set_gmail_status(user_id, "error", f"temporary refresh failure ({code or 'transient'})")
        raise GmailError(
            "Gmail authorization could not be refreshed right now",
            status="error",
            retryable=True,
        ) from None
    except (httpx.HTTPError, OSError):
        db.set_gmail_status(user_id, "error", "network failure while refreshing Gmail access")
        raise GmailError(
            "Gmail authorization could not be refreshed right now",
            status="error",
            retryable=True,
        ) from None

    access = str(payload.get("access_token") or "")
    if not access:
        db.set_gmail_status(user_id, "error", "refresh returned no access token")
        raise GmailError(
            "Gmail authorization could not be refreshed right now",
            status="error",
            retryable=True,
        )

    expires_in = float(payload.get("expires_in") or 0)
    db.update_gmail_tokens(
        user_id=user_id,
        access_token_enc=auth_service.encrypt_secret(access),
        token_expiry=time.time() + expires_in,
    )
    return access
