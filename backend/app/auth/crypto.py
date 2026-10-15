"""Authenticated encryption for values that must survive at rest.

Refresh tokens and access tokens are the most sensitive thing this process
stores, so they are never written in plaintext.  ``APP_ENCRYPTION_KEY`` in the
environment is the only input; the actual AES key is derived from it with
SHA-256, so any human-chosen string is accepted without weakening the cipher's
key length.

The format is self-describing and versioned::

    v1.<base64url nonce>.<base64url ciphertext+tag>

A fresh random nonce is generated for every value (AES-GCM requires it, and
reusing one with the same key is catastrophic), and the tag is verified on
decrypt, so a tampered or truncated value raises rather than returning junk.

If no key is configured, ``encrypt`` raises instead of falling back to
plaintext.  Failing closed is the point: an operator who has not set the key
must notice, not silently store Gmail refresh tokens unprotected.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "v1"
_NONCE_BYTES = 12


class EncryptionError(RuntimeError):
    """Raised when a value cannot be encrypted or decrypted safely."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _key_from(secret: str) -> bytes:
    if not secret:
        raise EncryptionError("APP_ENCRYPTION_KEY is not configured")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def encrypt(plaintext: str, key: str) -> str:
    """Encrypt ``plaintext`` under ``key`` with a fresh nonce."""
    aes = AESGCM(_key_from(key))
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"{_PREFIX}.{_b64e(nonce)}.{_b64e(ciphertext)}"


def decrypt(token: str, key: str) -> str:
    """Decrypt a value produced by :func:`encrypt`.  Raises on tampering."""
    if not token or not token.startswith(_PREFIX + "."):
        raise EncryptionError("value is not in the expected encrypted format")
    try:
        head, nonce_b64, ct_b64 = token.split(".", 2)
    except ValueError as exc:  # pragma: no cover - guarded by the prefix check
        raise EncryptionError("malformed encrypted value") from exc
    if head != _PREFIX:
        raise EncryptionError(f"unsupported encryption version {head!r}")
    aes = AESGCM(_key_from(key))
    try:
        plaintext = aes.decrypt(_b64d(nonce_b64), _b64d(ct_b64), None)
    except (InvalidTag, ValueError) as exc:
        raise EncryptionError("encrypted value failed authentication") from exc
    return plaintext.decode("utf-8")


def maybe_decrypt(token: str, key: str) -> Optional[str]:
    """Decrypt if possible, otherwise ``None`` -- for best-effort reads."""
    try:
        return decrypt(token, key)
    except EncryptionError:
        return None
