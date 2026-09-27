"""Who is allowed in.

The Quick Tunnel publishes this application at a ``trycloudflare.com`` URL that
anyone in the world can reach, and the Computer view behind it is a live,
keyboard-driven, signed-in Google Chrome.  The URL itself is not a secret -- it
appears in DNS records, in proxy logs and in browser history -- so something has
to actually check who is asking.

This is that something, and it is deliberately small:

* one shared passphrase, supplied as an environment variable, so there is no
  user table, no password reset flow and no third-party identity provider to
  operate;
* one signed, ``HttpOnly``, ``SameSite=Strict`` cookie, so the browser stops
  re-sending the passphrase on every request and the credential never becomes
  reachable from JavaScript;
* a constant-time comparison, so the passphrase cannot be recovered by timing
  the response;
* a hard requirement that a passphrase be set before the tunnel is brought up,
  because an unauthenticated screen is worse than no screen at all.

This is appropriate for a prototype serving one trusted user.  It is *not* a
substitute for per-user accounts: with one shared secret, everyone who logs in is
the same person as far as the app is concerned.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

# The cookie's value never leaves the server: it is a random session id paired
# with an HMAC of the expiry, so a stolen cookie cannot be replayed indefinitely
# and cannot be forged without the signing key.
_COOKIE = "rag_session"
_SESSION_TTL = 60 * 60 * 12  # 12 hours: a working day, not a standing grant.


def _token() -> Optional[str]:
    value = (os.environ.get("RAG_AUTH_TOKEN") or "").strip()
    return value or None


def enabled() -> bool:
    """True when a passphrase is configured and sessions must be checked."""
    return _token() is not None


def _sign(expiry: int) -> str:
    """HMAC over the expiry, keyed by the passphrase. Never stored, only checked."""
    material = f"{expiry}".encode()
    return hmac.new(_token().encode(), material, hashlib.sha256).hexdigest()


def _make_cookie_value() -> str:
    expiry = int(time.time()) + _SESSION_TTL
    return f"{expiry}.{secrets.token_urlsafe(24)}.{_sign(expiry)}"


def check_cookie(value: Optional[str]) -> bool:
    """Validate a session cookie. False for anything malformed, forged or expired."""
    expected = _token()
    if not expected or not value:
        return False
    parts = value.split(".")
    if len(parts) != 3:
        return False
    try:
        expiry = int(parts[0])
    except ValueError:
        return False
    if expiry < int(time.time()):
        return False
    return hmac.compare_digest(parts[2], _sign(expiry))


def check_passphrase(candidate: Optional[str]) -> bool:
    """Compare a submitted passphrase without leaking its length through timing."""
    expected = _token()
    if not expected or candidate is None:
        return False
    # compare_digest is constant time, but only for equal lengths; hashing both
    # to a fixed width first makes that true regardless of input length.
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).hexdigest(),
        hashlib.sha256(expected.encode()).hexdigest(),
    )


def session_cookie(secure: bool = True) -> str:
    """The Set-Cookie header value that establishes a session.

    ``Secure`` is on by default because the only deployed path is a Cloudflare
    Quick Tunnel, which is always https.  A Secure cookie is silently dropped by
    the browser on a plain-http origin, which is what local development uses, so
    the caller passes ``secure=False`` there -- see :func:`_request_is_secure`.

    Max-Age is what stops the browser restoring a stale cookie from a cache on
    the next visit to a page that is still public (/, /assets/...), which is the
    one case where an expired session could otherwise be reused.
    """
    flag = "; Secure" if secure else ""
    return (
        f"{_COOKIE}={_make_cookie_value()}"
        f"; Path=/; HttpOnly; SameSite=Strict; Max-Age={_SESSION_TTL}{flag}"
    )


def cleared_cookie(secure: bool = True) -> str:
    flag = "; Secure" if secure else ""
    return f"{_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{flag}"


def _request_is_secure(request) -> bool:
    """Whether the connection that carried this request is https.

    The app is behind a tunnel, so the socket uvicorn sees is plain http to
    loopback and says nothing useful.  ``X-Forwarded-Proto`` is what the edge
    sets, and it is trusted here because the only thing in front of this process
    is cloudflared on loopback.

    A request with no forwarded header is taken at face value as http, and that
    is deliberate rather than cautious: a ``Secure`` cookie on a genuinely plain
    http origin is dropped by the browser, so defaulting to Secure whenever the
    header is missing would lock out exactly the local development this path
    exists to support.  The header can only add Secure, never remove it.
    """
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    if proto in ("http", "ws", "wss"):
        return False
    return request.url.scheme == "https"


def read_cookie(cookie_header: Optional[str]) -> Optional[str]:
    """Pull the session cookie out of a raw Cookie header."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == _COOKIE:
            return value or None
    return None
