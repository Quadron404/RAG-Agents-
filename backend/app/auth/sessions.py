"""Server-side sessions with hashed identifiers.

A session id is a 256-bit random token handed to the browser in an HttpOnly
cookie.  The database stores only an HMAC of it, so a leaked database read does
not yield usable cookies.  Verification recomputes the HMAC and looks it up by
constant-time comparison, then checks expiry and revocation.

Sessions are rotated after a successful login and after consent changes: the
pre-login (anonymous/CSRF-only) session is replaced by a fresh authenticated
one, which is what closes session-fixation.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from ..db import Database

COOKIE_NAME = "rag_session"
CSRF_HEADER = "x-rag-csrf"
DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session(token: str, secret: str) -> str:
    """HMAC-SHA256 of the session token under the server secret."""
    if not secret:
        raise ValueError("session secret is not configured")
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


class SessionService:
    """Thin typed layer over the ``sessions`` table."""

    def __init__(self, db: Database, secret: str, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.db = db
        self.secret = secret
        self.ttl = ttl_seconds

    def create(self, user_id: str, *, rotated_from: Optional[str] = None) -> dict:
        token = new_session_token()
        csrf = new_csrf_token()
        record = self.db.create_session(
            session_hash=hash_session(token, self.secret),
            csrf=csrf,
            user_id=user_id,
            ttl_seconds=self.ttl,
            rotated_from=rotated_from,
        )
        record["token"] = token
        return record

    def resolve(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        return self.db.get_valid_session(hash_session(token, self.secret))

    def revoke(self, token: Optional[str]) -> bool:
        if not token:
            return False
        return self.db.revoke_session(hash_session(token, self.secret))

    def revoke_user(self, user_id: str) -> int:
        return self.db.revoke_sessions_for_user(user_id)
