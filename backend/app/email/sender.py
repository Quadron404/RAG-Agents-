from __future__ import annotations

import base64
import json
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple

import httpx

from ..auth import google as google_oauth
from ..auth import crypto

#: The Gmail send endpoint, addressed to the authenticated sender's own mailbox
#: (``me``), never to any address taken from a tool call or message.
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class BotSenderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        configured: bool = False,
        retryable: bool = True,
        needs_reconnect: bool = False,
    ) -> None:
        super().__init__(message)
        self.configured = bool(configured)
        self.retryable = bool(retryable)
        self.needs_reconnect = bool(needs_reconnect)


class BotSender:
    """Dedicated bot-owned sender. Never uses the owner's connected Gmail.

    Configured via settings:
    - bot_sender_provider: "gmail" or "smtp"
    - bot_sender_email: From address
    - gmail: bot_sender_refresh_token (encrypted v1.* or plaintext in env; decrypted via APP_ENCRYPTION_KEY)
    - smtp: bot_sender_smtp_host/user/password
    """

    def __init__(self, settings):
        self.settings = settings

    def configured(self) -> bool:
        try:
            self._resolve_provider()
            return True
        except BotSenderError:
            return False

    def describe(self) -> dict:
        provider = ""
        try:
            provider = self._resolve_provider()
        except BotSenderError:
            provider = ""
        return {
            "provider": provider,
            "sender_email": self.settings.bot_sender_email or "",
            "configured": self.configured(),
        }

    async def send(
        self,
        recipient_email: str,
        subject: str,
        body: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> str:
        """Send a notification. Returns provider message id (may be empty for SMTP).

        Never logs credentials/tokens. Never allows recipient override.
        """
        if not recipient_email or not subject:
            raise BotSenderError("recipient and subject required", configured=self.configured(), retryable=False)

        provider = self._resolve_provider()
        if provider == "gmail":
            return await self._send_gmail(recipient_email, subject, body, client)
        if provider == "smtp":
            return await self._send_smtp(recipient_email, subject, body)
        raise BotSenderError(f"unknown provider {provider}", configured=False, retryable=False)

    # --- internals ---------------------------------------------------------

    def _resolve_provider(self) -> str:
        p = self.settings.bot_sender_provider
        if not p:
            if self.settings.bot_sender_refresh_token:
                return "gmail"
            if self.settings.bot_sender_smtp_host:
                return "smtp"
        if p not in ("gmail", "smtp"):
            raise BotSenderError(f"invalid bot sender provider {p}", configured=False, retryable=False)
        if p == "gmail":
            if not self.settings.bot_sender_email:
                raise BotSenderError("bot_sender_email required for gmail", configured=False, retryable=False)
            if not self.settings.bot_sender_refresh_token:
                raise BotSenderError("bot_sender_refresh_token required for gmail", configured=False, retryable=False)
        if p == "smtp":
            if not self.settings.bot_sender_email or not self.settings.bot_sender_smtp_host:
                raise BotSenderError("bot sender SMTP config missing", configured=False, retryable=False)
        return p

    def _decrypt_secret(self, raw: str) -> str:
        if not raw:
            return ""
        if raw.startswith("v1."):
            d = crypto.maybe_decrypt(raw, self.settings.app_encryption_key)
            if d is None:
                raise BotSenderError("could not decrypt bot sender secret; check APP_ENCRYPTION_KEY", configured=True, retryable=False, needs_reconnect=True)
            return d
        return raw

    async def _send_gmail(
        self,
        recipient_email: str,
        subject: str,
        body: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> str:
        refresh = self._decrypt_secret(self.settings.bot_sender_refresh_token)
        try:
            payload = await google_oauth.refresh_access_token(
                settings=self.settings, refresh_token=refresh, client=client
            )
        except google_oauth.OAuthError as exc:
            code = str(getattr(exc, "error_code", "") or "")
            perm = not getattr(exc, "retryable", True) or code in google_oauth.PERMANENT_ERROR_CODES
            raise BotSenderError(
                "bot sender authorization invalid",
                configured=True,
                retryable=not perm,
                needs_reconnect=perm,
            ) from None
        except Exception:
            # The exception text is deliberately not echoed: an upstream error can
            # quote the request, and the request carries the credential.
            raise BotSenderError("bot sender refresh failed", configured=True, retryable=True) from None

        access = str(payload.get("access_token") or "")
        if not access:
            raise BotSenderError("bot sender refresh returned no token", configured=True, retryable=True)

        mime = self._build_mime(self.settings.bot_sender_email, recipient_email, subject, body)
        # The Gmail API takes the whole RFC 822 message base64url-encoded.  The
        # From header is the configured bot sender, the To header the resolved
        # owner -- neither is ever derived from model output.
        raw = base64.urlsafe_b64encode(mime.encode("utf-8")).decode("utf-8").replace("=", "")
        headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        async with (client if client is not None else httpx.AsyncClient(timeout=30.0)) as c:
            resp = await c.post(GMAIL_SEND_URL, headers=headers, content=json.dumps({"raw": raw}))
            if resp.status_code >= 500:
                raise BotSenderError(f"Gmail API error {resp.status_code}", configured=True, retryable=True)
            if resp.status_code >= 400:
                raise BotSenderError(f"Gmail API rejected message {resp.status_code}", configured=True, retryable=False)
            try:
                data = resp.json()
            except Exception:
                data = {}
        return str(data.get("id") or "")

    def _build_mime(self, sender: str, to: str, subject: str, body: str) -> str:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        return msg.as_string()

    async def _send_smtp(self, recipient_email: str, subject: str, body: str) -> str:
        user = self.settings.bot_sender_smtp_user or self.settings.bot_sender_email
        pwd = self._decrypt_secret(self.settings.bot_sender_smtp_password)
        port = int(self.settings.bot_sender_smtp_port or 587)
        host = self.settings.bot_sender_smtp_host
        if not host:
            raise BotSenderError("SMTP host missing", configured=False, retryable=False)
        msg = MIMEMultipart()
        msg["From"] = self.settings.bot_sender_email
        msg["To"] = recipient_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        try:
            if self.settings.bot_sender_smtp_use_ssl and port == 465:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as s:
                    if pwd:
                        s.login(user, pwd)
                    s.sendmail(self.settings.bot_sender_email, [recipient_email], msg.as_string())
            else:
                with smtplib.SMTP(host, port, timeout=30) as s:
                    s.ehlo()
                    if self.settings.bot_sender_smtp_use_tls:
                        s.starttls(context=ssl.create_default_context())
                        s.ehlo()
                    if pwd:
                        s.login(user, pwd)
                    s.sendmail(self.settings.bot_sender_email, [recipient_email], msg.as_string())
        except smtplib.SMTPAuthenticationError:
            # The bot account's credential is wrong: retrying with the same
            # password cannot help, so this is terminal until an admin fixes it.
            raise BotSenderError(
                "SMTP authentication rejected by the server", configured=True, retryable=False, needs_reconnect=True
            ) from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
            raise BotSenderError("SMTP refused the message", configured=True, retryable=False) from None
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, OSError):
            # A refused connection, a dropped session or a timeout is the
            # server's "try later", not a fault of the message.  The raw
            # exception is not echoed.
            raise BotSenderError("SMTP send failed (transient)", configured=True, retryable=True) from None
        except Exception:
            raise BotSenderError("SMTP send failed", configured=True, retryable=True) from None
        return ""  # no provider message id from SMTP in the simple case

