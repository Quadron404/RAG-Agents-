"""Google OpenID Connect: the authorization-code flow with PKCE.

Two distinct grants share this code but request different scopes:

- **login** requests only ``openid email profile``.  Signing in never asks for
  Gmail, so a user who only wants an account is never pushed into granting
  mailbox access.
- **gmail** requests ``gmail.send`` and ``gmail.readonly`` on top, and is only
  offered from an already-authenticated session as an explicit, separate
  consent step.  The callback binds the granted mailbox to the same Google
  subject that owns the session; a different account selected at consent time
  is rejected.

Token exchange and the JWKS fetch are the only network calls, both injected so
they can be tested without a socket.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx

from .tokens import TokenError, verify_id_token

LOGIN_SCOPES = ("openid", "email", "profile")
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)
#: The original consent also grants the OIDC scopes, because the same client is
#: used and an access token is needed to call the Gmail API.
GMAIL_FULL_SCOPES = LOGIN_SCOPES + GMAIL_SCOPES


#: OAuth error codes that will never succeed on a retry: the grant is gone, was
#: never granted, or the client is misconfigured.  A caller that sees one of
#: these must stop and ask the user to reconnect rather than retrying forever --
#: ``invalid_grant`` is exactly what Google returns once a refresh token has been
#: revoked or has expired, and retrying it can never change the answer.
PERMANENT_ERROR_CODES = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope"}
)


class OAuthError(RuntimeError):
    """A token endpoint or revocation call failed.

    Carries the provider's own error code and whether retrying could ever help,
    so callers do not have to guess from a message string.  It never carries the
    request body (which can contain a client secret) or a token.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        status: int = 0,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = int(status)
        self.retryable = bool(retryable)


def _oauth_error_from_body(status: int, body: object, prefix: str) -> OAuthError:
    """Build an :class:`OAuthError` from a token endpoint error response.

    Only ``error`` and ``error_description`` are read -- never the whole body,
    which is where a leaked client secret would appear.  The description is
    truncated because an error body is not a log line.
    """
    code = ""
    description = ""
    if isinstance(body, dict):
        code = str(body.get("error") or "").strip()
        description = str(body.get("error_description") or "").strip()[:200]
    retryable = code not in PERMANENT_ERROR_CODES and status != 400
    message = f"{prefix} with HTTP {status}"
    if code:
        message += f" ({code})"
    if description:
        message += f": {description}"
    return OAuthError(message, error_code=code, status=status, retryable=retryable)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def scopes_for(mode: str) -> Tuple[str, ...]:
    return GMAIL_FULL_SCOPES if mode == "gmail" else LOGIN_SCOPES


def build_authorization_url(
    *,
    settings,
    redirect_uri: str,
    state: str,
    nonce: str,
    verifier: str,
    mode: str = "login",
    login_hint: str = "",
) -> str:
    """The URL the browser is sent to for consent.

    ``prompt=consent`` is requested only for the Gmail grant, so reconnecting a
    revoked mailbox always shows the consent screen, while ordinary sign-in can
    be silent when the browser already has a session with Google.
    """
    scope = " ".join(scopes_for(mode))
    query = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "access_type": "offline" if mode == "gmail" else "online",
        "include_granted_scopes": "true",
    }
    if mode == "gmail":
        query["prompt"] = "consent"
    if login_hint:
        query["login_hint"] = login_hint
    return f"{settings.google_auth_url}?{urlencode(query)}"


async def exchange_code(
    *,
    settings,
    code: str,
    redirect_uri: str,
    verifier: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, object]:
    """Swap an authorization code for tokens."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(
            settings.google_token_url,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise _oauth_error_from_body(resp.status_code, _safe_json(resp), "token exchange failed")
    payload = resp.json()
    if "id_token" not in payload and "access_token" not in payload:
        raise OAuthError("token response carried neither an id_token nor an access_token")
    return payload


async def refresh_access_token(
    *,
    settings,
    refresh_token: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, object]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(
            settings.google_token_url,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        # ``invalid_grant`` here means the refresh token was revoked or expired;
        # the caller must treat that as permanent (reconnect), not transient.
        raise _oauth_error_from_body(resp.status_code, _safe_json(resp), "refresh failed")
    return resp.json()


async def revoke_token(
    *,
    settings,
    token: str,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Best-effort revocation of a refresh or access token at Google.

    Returns True only when Google accepted the revocation (HTTP 200).  A network
    error or a non-200 is reported as False rather than raised: the local
    connection is removed either way, and a failure to revoke remotely must not
    make "disconnect" look like it failed.  The token is never logged.
    """
    if not token:
        return False
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(
            settings.google_revoke_url,
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except (httpx.HTTPError, OSError):
        return False
    finally:
        if own:
            await client.aclose()
    return resp.status_code == 200


def _safe_json(resp) -> object:
    """The parsed error body, or ``{}`` if it is not JSON.

    Kept small on purpose: the caller only reads ``error`` and
    ``error_description`` from it, so an unparseable or huge body is discarded
    rather than carried around.
    """
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        return {}


async def fetch_jwks(settings, client: Optional[httpx.AsyncClient] = None) -> Dict[str, object]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(settings.google_jwks_url, headers={"Accept": "application/json"})
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise OAuthError(f"JWKS fetch failed with HTTP {resp.status_code}")
    return resp.json()


def verify_login_token(settings, id_token: str, jwks: Dict[str, object], nonce: str) -> Dict[str, object]:
    """Validate an ID token from the login consent against everything we know."""
    try:
        return verify_id_token(
            id_token,
            jwks=jwks,
            audience=settings.google_client_id,
            nonce=nonce,
        )
    except TokenError as exc:
        raise OAuthError(str(exc)) from exc


def pkce_pair() -> Tuple[str, str]:
    verifier = new_code_verifier()
    return verifier, code_challenge(verifier)


def now() -> float:
    return time.time()
