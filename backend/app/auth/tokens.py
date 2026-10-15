"""Verification of Google identity tokens, implemented against the spec.

This is deliberately not a general JWT library.  Google ID tokens are always
RS256, and the only claims this application trusts are the ones it validates
here.  Keeping the check explicit means every rule is visible and testable:

- the header algorithm must be RS256 (never ``none``, never a symmetric alg);
- the signature must verify against the key Google names by ``kid``;
- ``iss`` must be Google's issuer;
- ``aud`` must be this application's client id;
- ``exp``/``iat`` must be present and sane;
- ``sub`` must be present (it is the only stable identity);
- ``email_verified`` must be true.

Nothing here trusts a frontend-supplied email address.  The subject claim is the
identity, and only the rule-validated token can produce it.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
#: Clock skew tolerated when checking iat/exp, in seconds.
DEFAULT_LEEWAY = 120


class TokenError(ValueError):
    """The token failed validation.  Never carries the token itself."""


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + pad)
    except (ValueError, TypeError) as exc:
        raise TokenError("segment is not valid base64url") from exc


def _json_segment(segment: str) -> Dict[str, Any]:
    try:
        value = json.loads(_b64url_decode(segment))
    except (TokenError, json.JSONDecodeError) as exc:
        raise TokenError("segment is not valid JSON") from exc
    if not isinstance(value, dict):
        raise TokenError("segment is not a JSON object")
    return value


def decode_unverified(token: str) -> Dict[str, Any]:
    """The header and payload without verifying anything.

    Used only to read ``kid`` and to report which issuer/audience a token named.
    Never treat its output as trusted.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("a JWT must have three segments")
    return {"header": _json_segment(parts[0]), "payload": _json_segment(parts[1])}


def public_key_from_jwk(jwk: Dict[str, Any]) -> rsa.RSAPublicKey:
    if jwk.get("kty") != "RSA":
        raise TokenError(f"unsupported key type {jwk.get('kty')!r}")
    try:
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    except KeyError as exc:
        raise TokenError("JWK is missing a modulus or exponent") from exc
    return rsa.RSAPublicNumbers(e, n).public_key()


def _find_key(jwks: Dict[str, Any], kid: Optional[str]) -> rsa.RSAPublicKey:
    keys: List[Dict[str, Any]] = list(jwks.get("keys") or [])
    if kid:
        keys = [k for k in keys if k.get("kid") == kid]
    if not keys:
        raise TokenError(f"no signing key matches kid {kid!r}")
    return public_key_from_jwk(keys[0])


def verify_signature(token: str, jwks: Dict[str, Any]) -> str:
    """Verify the RS256 signature.  Returns the ``kid`` that matched."""
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("a JWT must have three segments")
    header = _json_segment(parts[0])
    if header.get("alg") != "RS256":
        raise TokenError(f"unsupported algorithm {header.get('alg')!r}")
    key = _find_key(jwks, header.get("kid"))
    signing_input = (parts[0] + "." + parts[1]).encode("ascii")
    signature = _b64url_decode(parts[2])
    try:
        key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise TokenError("signature does not verify") from exc
    return str(header.get("kid") or "")


def verify_id_token(
    token: str,
    *,
    jwks: Dict[str, Any],
    audience: str,
    nonce: Optional[str] = None,
    now: Optional[float] = None,
    leeway: int = DEFAULT_LEEWAY,
) -> Dict[str, Any]:
    """Fully validate a Google ID token and return its claims.

    Raises :class:`TokenError` on any failure; callers map that to an
    authentication failure and never to a partial success.
    """
    if not audience:
        raise TokenError("no client id configured to validate against")
    current = now if now is not None else time.time()
    verify_signature(token, jwks)
    parts = token.split(".")
    claims = _json_segment(parts[1])

    iss = claims.get("iss")
    if iss not in GOOGLE_ISSUERS:
        raise TokenError(f"unexpected issuer {iss!r}")

    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if audience not in audiences:
        raise TokenError("audience does not match this client id")

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise TokenError("token has no expiration")
    if current > exp + leeway:
        raise TokenError("token has expired")

    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and iat - leeway > current:
        raise TokenError("token was issued in the future")

    if not claims.get("sub"):
        raise TokenError("token has no subject identifier")

    if nonce is not None and claims.get("nonce") != nonce:
        raise TokenError("nonce does not match the authorization request")

    if claims.get("email") and claims.get("email_verified") is not True:
        raise TokenError("email is present but not verified")

    return claims
