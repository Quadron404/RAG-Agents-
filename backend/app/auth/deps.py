"""FastAPI-facing authentication: the dependencies every route uses.

One object, :class:`AuthService`, owns the two questions a protected route asks:

1. *Did this request come through the trusted gateway?*  When
   ``RAG_INTERNAL_TOKEN`` is configured, a request must carry it.  The token is
   added by the Cloudflare Worker on the way through and never reaches the
   browser, so holding a Quick Tunnel URL is not enough to reach the API.
2. *Who is the user?*  There are two equally valid answers, and which one
   applies depends on who is holding the session:

   * Behind the Worker, the edge owns Google sign-in and the session cookie.
     It strips the browser's cookie and asserts the verified identity in
     ``x-rag-user-sub`` and friends; this backend then owns a user row keyed by
     that Google subject.  Because the same request already proved it came from
     the gateway, and the edge enforces CSRF before proxying, an edge-asserted
     identity does not carry a backend CSRF token.
   * In local development, with no Worker in front, the identity comes from a
     valid session cookie that maps to a stored session row and then to a user
     record.

   A ``user_id`` in the query string, body or path is ignored everywhere.

CSRF is enforced on state-changing methods by comparing a per-session token,
which the frontend reads from ``/auth/session`` and echoes in a header.  The
edge-asserted identity is exempt here only because the Worker rejects a
state-changing proxy request that lacks the matching edge CSRF token first.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from ..config import Settings
from ..db import Database
from . import crypto
from .sessions import COOKIE_NAME, CSRF_HEADER, SessionService

INTERNAL_HEADER = "x-rag-internal-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Identity a trusted Worker asserts about the signed-in user, on the way
#: through the proxy.  Only ever read after the internal token has been checked;
#: a client that can set these headers without the token reaches nothing.
IDENTITY_SUB_HEADER = "x-rag-user-sub"
IDENTITY_EMAIL_HEADER = "x-rag-user-email"
IDENTITY_EMAIL_VERIFIED_HEADER = "x-rag-user-email-verified"
IDENTITY_NAME_HEADER = "x-rag-user-name"
IDENTITY_PICTURE_HEADER = "x-rag-user-picture"


@dataclass
class Principal:
    user: dict
    session: dict
    #: True when the identity was asserted by the trusted Worker rather than
    #: resolved from this backend's own session cookie.  The edge owns CSRF for
    #: these requests, so the backend does not hold a matching token to check.
    via_gateway: bool = False

    @property
    def user_id(self) -> str:
        return str(self.user["id"])


class AuthService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._session_secret = (
            settings.session_secret
            or settings.app_encryption_key
            or "rag-dev-session-secret-change-me"
        )
        self.sessions = SessionService(db, self._session_secret, settings.session_ttl_seconds)

    # --- gateway ----------------------------------------------------------

    @property
    def internal_configured(self) -> bool:
        return bool(self.settings.rag_internal_token)

    def internal_token_valid(self, request: Request) -> bool:
        supplied = request.headers.get(INTERNAL_HEADER, "")
        expected = self.settings.rag_internal_token
        if not expected:
            return False
        return hmac.compare_digest(supplied, expected)

    def websocket_internal_valid(self, token: Optional[str]) -> bool:
        expected = self.settings.rag_internal_token
        if not expected:
            return False
        return hmac.compare_digest(token or "", expected)

    def require_gateway(self, request: Request) -> None:
        """Reject a request that did not arrive through the trusted gateway.

        A no-op while no internal token is configured (local development).  Once
        one is set, every protected HTTP route insists on it.
        """
        if not self.internal_configured:
            return
        if not self.internal_token_valid(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="internal authentication required",
            )

    # --- identity ---------------------------------------------------------

    def cookie_token(self, request: Request) -> Optional[str]:
        return request.cookies.get(COOKIE_NAME)

    def asserted_principal(self, headers) -> Optional[Principal]:
        """Build the user for a Google subject the trusted Worker asserted.

        Called only once the internal token has been verified, so the headers
        are trustworthy.  The subject is the identity: the backend finds or
        creates its own user row for it, which is what keeps threads and
        ownership consistent whether the request arrived through the Worker or
        from a local cookie session.
        """
        sub = (headers.get(IDENTITY_SUB_HEADER) or "").strip()
        if not sub:
            return None
        verified = (headers.get(IDENTITY_EMAIL_VERIFIED_HEADER) or "").strip().lower()
        user = self.db.upsert_user(
            sub,
            email=headers.get(IDENTITY_EMAIL_HEADER, "") or "",
            email_verified=verified in ("1", "true", "yes", "on"),
            name=headers.get(IDENTITY_NAME_HEADER, "") or "",
            picture=headers.get(IDENTITY_PICTURE_HEADER, "") or "",
        )
        return Principal(
            user=user,
            session={"csrf": "", "user_id": str(user["id"])},
            via_gateway=True,
        )

    def gateway_principal(self, request: Request) -> Optional[Principal]:
        """The asserted identity, but only when the gateway token checks out."""
        if not self.internal_token_valid(request):
            return None
        return self.asserted_principal(request.headers)

    def _principal_from_token(self, token: Optional[str]) -> Optional[Principal]:
        session = self.sessions.resolve(token)
        if not session:
            return None
        user = self.db.get_user(str(session["user_id"]))
        if not user:
            return None
        return Principal(user=user, session=session)

    def optional_principal(self, request: Request) -> Optional[Principal]:
        asserted = self.gateway_principal(request)
        if asserted:
            return asserted
        return self._principal_from_token(self.cookie_token(request))

    def principal_from_token(self, token: Optional[str]) -> Optional[Principal]:
        return self._principal_from_token(token)

    def require_principal(self, request: Request) -> Principal:
        self.require_gateway(request)
        principal = self.optional_principal(request)
        if not principal:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
        # The edge enforces CSRF for proxied requests, so an asserted identity
        # is exempt here; a cookie session must still echo its own token.
        if request.method not in SAFE_METHODS and not principal.via_gateway:
            self._check_csrf(request, principal)
        return principal

    def _check_csrf(self, request: Request, principal: Principal) -> None:
        supplied = request.headers.get(CSRF_HEADER, "")
        expected = str(principal.session.get("csrf", ""))
        if not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")

    # --- secrets ----------------------------------------------------------

    def encrypt_secret(self, value: str) -> str:
        return crypto.encrypt(value, self.settings.app_encryption_key)

    def decrypt_secret(self, value: str) -> Optional[str]:
        return crypto.maybe_decrypt(value, self.settings.app_encryption_key)
