"""Authentication and per-user isolation, against the real app.

Phase 1 of the security work.  These tests prove the rules that the live app's
usability depends on, each over the real ASGI stack unless stated otherwise:

- a request without a valid session is denied, on HTTP and on WebSocket;
- a client cannot spoof another user's id;
- one user cannot read or mutate another user's threads;
- sessions expire and logout revokes them;
- CSRF is enforced on state-changing requests;
- Google identity tokens are validated properly (signature, issuer, audience,
  exp/iat, nonce, verified email);
- the Gmail binding rejects a different Google account;
- pre-authentication conversations are quarantined, never handed to a new user.

Run with:  python -m tests.test_auth
"""
from __future__ import annotations

import base64
import json
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# Imported before app.main so the environment (DATA_DIR, secrets) is configured
# before the app reads it.
import tests.support as support

import app.main as main_module
from app.auth import tokens
from app.auth.sessions import COOKIE_NAME

_REDIRECTS = (302, 307)  # RedirectResponse may roll over to a re-dispatch


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_byte_len(n: int) -> int:
    return (n.bit_length() + 7) // 8


def make_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks_for(public_key, kid: str = "k1") -> dict:
    nums = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64u(nums.n.to_bytes(_b64u_byte_len(nums.n), "big")),
                "e": _b64u(nums.e.to_bytes(_b64u_byte_len(nums.e), "big")),
            }
        ]
    }


def sign_jwt(private_key, header: dict, payload: dict) -> str:
    enc_head = _b64u(json.dumps(header, separators=(",", ":")).encode())
    enc_payload = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{enc_head}.{enc_payload}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{enc_head}.{enc_payload}.{_b64u(signature)}"


def google_id_token(private_key, *, sub="sub-1", iss="https://accounts.google.com",
                    aud="client-id", exp=None, iat=None, nonce=None, email="alice@example.com",
                    email_verified=True, kid="k1") -> str:
    now = int(time.time())
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": iat if iat is not None else now - 60,
        "exp": exp if exp is not None else now + 3600,
        "azp": aud,
    }
    if email is not None:
        payload["email"] = email
        payload["email_verified"] = email_verified
    if nonce is not None:
        payload["nonce"] = nonce
    return sign_jwt(private_key, {"alg": "RS256", "typ": "JWT", "kid": kid}, payload)


class TestGoogleTokenValidation(unittest.TestCase):
    def setUp(self):
        self.key = make_keypair()
        self.jwks = jwks_for(self.key.public_key())

    def test_valid_token_passes(self):
        token = google_id_token(self.key, nonce="n-1")
        claims = tokens.verify_id_token(token, jwks=self.jwks, audience="client-id", nonce="n-1")
        self.assertEqual(claims["sub"], "sub-1")

    def test_unknown_kid_is_rejected(self):
        token = google_id_token(self.key, kid="missing")
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_wrong_algorithm_is_rejected(self):
        token = sign_jwt(
            self.key, {"alg": "none", "typ": "JWT"}, {"iss": "x", "aud": "client-id", "sub": "s", "exp": 2**40}
        )
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_tampered_signature_is_rejected(self):
        token = google_id_token(self.key)
        token = token[: len(token) // 2] + ("AA" if not token.endswith("A") else "BB") + token[len(token) // 2 + 2:]
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_wrong_issuer_is_rejected(self):
        token = google_id_token(self.key, iss="https://evil.example.com")
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_wrong_audience_is_rejected(self):
        token = google_id_token(self.key, aud="other-client")
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_expired_token_is_rejected(self):
        token = google_id_token(self.key, exp=int(time.time()) - 300)
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_future_issued_token_is_rejected(self):
        token = google_id_token(self.key, iat=int(time.time()) + 3600)
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_nonce_mismatch_is_rejected(self):
        token = google_id_token(self.key, nonce="other")
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id", nonce="expected")

    def test_unverified_email_is_rejected(self):
        token = google_id_token(self.key, email_verified=False)
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")

    def test_missing_subject_is_rejected(self):
        token = google_id_token(self.key, sub="")
        with self.assertRaises(tokens.TokenError):
            tokens.verify_id_token(token, jwks=self.jwks, audience="client-id")


def _configure_google(settings_setter=None):
    cfg = main_module.settings
    cfg.google_client_id = "client-id"
    cfg.google_client_secret = "client-secret"
    cfg.public_base_url = "https://app.example.workers.dev"
    if settings_setter:
        settings_setter(cfg)


class TestHttpAccessControl(unittest.TestCase):
    def setUp(self):
        self.alice = support.sign_in("alice-sub", "alice@example.com")

    def test_unauthenticated_requests_are_denied(self):
        # Local-dev mode: no internal token configured, so the only credential
        # is the session cookie -- and a caller without one is denied everywhere.
        main_module.settings.rag_internal_token = ""
        from fastapi.testclient import TestClient

        client = TestClient(main_module.app)
        for method, path, body in (
            ("GET", "/threads", None),
            ("POST", "/threads", {"title": "x"}),
            ("GET", "/screen/config", None),
            ("GET", "/ai/computer/providers", None),
            ("GET", "/sysinfo", None),
        ):
            resp = client.request(method, path, json=body)
            self.assertEqual(resp.status_code, 401, f"{method} {path} should be denied")

    def test_direct_external_call_without_internal_token_is_denied(self):
        _configure_google()
        client, _ = support.make_client(self.alice["id"])  # internal token configured
        # A stranger with no token and no cookie: rejected by the gateway first.
        from fastapi.testclient import TestClient

        stranger = TestClient(main_module.app)
        resp = stranger.get("/threads")
        self.assertEqual(resp.status_code, 401)
        # Even with a valid session cookie but no internal token (a direct call
        # to the tunnel, not through the Worker): denied.
        session = main_module.auth.sessions.create(self.alice["id"])
        direct = TestClient(main_module.app)
        direct.cookies.set(COOKIE_NAME, str(session["token"]))
        self.assertEqual(direct.get("/threads").status_code, 401)

    def test_websocket_without_session_is_denied(self):
        from fastapi.testclient import TestClient

        main_module.settings.rag_internal_token = ""
        client = TestClient(main_module.app)
        with self.assertRaises(Exception):
            with client.websocket_connect("/ws/me"):
                pass

    def test_client_cannot_spoof_user_id(self):
        _configure_google()
        client, _ = support.make_client(self.alice["id"])
        resp = client.post("/threads", json={"title": "hidden", "user_id": "victim-user"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user_id"], self.alice["id"], "the server must overwrite the claimed id")
        thread_id = body["thread_id"]
        # A different user never sees it in their own list.
        bob = support.sign_in("bob-sub", "bob@example.com")
        bob_client, _ = support.make_client(bob["id"])
        bob_threads = bob_client.get("/threads").json()["threads"]
        self.assertNotIn(thread_id, [t["id"] for t in bob_threads])

    def test_user_cannot_read_another_users_thread(self):
        _configure_google()
        alice_client, _ = support.make_client(self.alice["id"])
        thread_id = alice_client.post("/threads", json={"title": "alice private"}).json()["thread_id"]
        bob = support.sign_in("bob-sub", "bob@example.com")
        bob_client, _ = support.make_client(bob["id"])
        resp = bob_client.get(f"/threads/{thread_id}/messages")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("error"), "thread not found")
        resp = bob_client.post(f"/threads/{thread_id}/send", json={"text": "hello"})
        self.assertEqual(resp.json().get("error"), "thread not found")

    def test_session_expiration_denies_access(self):
        _configure_google()
        import time as _t

        from app.auth.sessions import hash_session

        now = _t.time()
        token = f"expired-token-{now}"
        main_module.db.create_session(
            hash_session(token, main_module.auth._session_secret),
            "csrf", self.alice["id"], 3600, rotated_from=None,
        )
        main_module.db.conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE session_hash = ?",
            (now - 10, hash_session(token, main_module.auth._session_secret)),
        )
        main_module.db.conn.commit()
        from fastapi.testclient import TestClient

        client = TestClient(main_module.app)
        client.cookies.set(COOKIE_NAME, token)
        self.assertEqual(client.get("/threads").status_code, 401)

    def test_logout_revokes_the_session(self):
        _configure_google()
        client, _ = support.make_client(self.alice["id"])
        self.assertEqual(client.get("/threads").status_code, 200)
        resp = client.post("/auth/logout")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client.get("/threads").status_code, 401)

    def test_state_changing_request_requires_csrf(self):
        _configure_google()
        from fastapi.testclient import TestClient

        client = TestClient(main_module.app)
        client.headers[support.INTERNAL_HEADER] = support.INTERNAL
        session = main_module.auth.sessions.create(self.alice["id"])
        client.cookies.set(COOKIE_NAME, str(session["token"]))
        # No CSRF header on a POST.
        self.assertEqual(client.post("/threads", json={"title": "x"}).status_code, 403)

    def test_correct_csrf_allows_the_mutation(self):
        _configure_google()
        client, session = support.make_client(self.alice["id"])
        self.assertIsNotNone(session["csrf"])
        resp = client.post("/threads", json={"title": "allowed"})
        self.assertEqual(resp.status_code, 200)


class TestOAuthLoginFlow(unittest.TestCase):
    def setUp(self):
        _configure_google()
        # The flow tests use a browser that goes straight to the public auth
        # endpoints without an internal token; the identity check is the cookie.
        main_module.settings.rag_internal_token = ""
        self.key = make_keypair()
        self.jwks = jwks_for(self.key.public_key())

    def _patch_google(self, id_token: str, extra=None):
        import unittest.mock as mock

        async def fake_exchange(*args, **kwargs):
            payload = {"id_token": id_token, "access_token": "at", "expires_in": 3600}
            payload.update(extra or {})
            return payload

        async def fake_fetch(*args, **kwargs):
            return self.jwks

        patch = mock.patch.multiple(
            main_module.google_oauth,
            exchange_code=fake_exchange,
            fetch_jwks=fake_fetch,
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_login_ends_in_a_valid_session(self):
        state = main_module.google_oauth.new_state()
        verifier = main_module.google_oauth.new_code_verifier()
        nonce = main_module.google_oauth.new_nonce()
        main_module.db.create_oauth_state(state, "login", "", verifier, nonce, "https://app.example.workers.dev/auth/callback")
        token = google_id_token(self.key, nonce=nonce, email="flow-alice@example.com")
        self._patch_google(token)

        from fastapi.testclient import TestClient

        client = TestClient(main_module.app)
        resp = client.get(f"/auth/callback?state={state}&code=abc", follow_redirects=False)
        self.assertIn(resp.status_code, _REDIRECTS, resp.text)
        # The session cookie is marked Secure (public base URL is https), and the
        # test client speaks http, so hand the token across the way a browser on
        # the real origin would.
        set_cookie = next(h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{COOKIE_NAME}="))
        raw_token = set_cookie.split(";", 1)[0].split("=", 1)[1]
        client.cookies.set(COOKIE_NAME, raw_token)
        session_resp = client.get("/auth/session").json()
        self.assertTrue(session_resp["authenticated"], "the login produced no authenticated session")
        self.assertEqual(session_resp["user"]["email"], "flow-alice@example.com")

    def test_gmail_consent_binds_only_the_same_google_identity(self):
        alice = main_module.db.upsert_user("gm-mismatch-sub", "alice@example.com", True, "Alice", "")
        client, _ = support.make_client(alice["id"])
        # Grant belongs to a different Google account than the session.
        state = main_module.google_oauth.new_state()
        verifier = main_module.google_oauth.new_code_verifier()
        nonce = main_module.google_oauth.new_nonce()
        main_module.db.create_oauth_state(state, "gmail", alice["id"], verifier, nonce, "https://app.example.workers.dev/auth/gmail/callback")
        wrong = google_id_token(self.key, sub="someone-else", nonce=nonce, email="eve@example.com")
        self._patch_google(wrong, extra={"refresh_token": "rt", "access_token": "at", "expires_in": 3600})
        resp = client.get(f"/auth/gmail/callback?state={state}&code=abc", follow_redirects=False)
        self.assertIn(resp.status_code, _REDIRECTS, resp.text)
        self.assertEqual(resp.headers.get("location", ""), "/?gmail=wrong_account")
        connection = main_module.db.get_gmail_connection(alice["id"])
        self.assertIsNone(connection, "a mismatched consent must not be stored")

    def test_gmail_consent_same_identity_stores_encrypted_tokens(self):
        alice = main_module.db.upsert_user("gm-ok-sub", "alice@example.com", True, "Alice", "")
        client, _ = support.make_client(alice["id"])
        state = main_module.google_oauth.new_state()
        verifier = main_module.google_oauth.new_code_verifier()
        nonce = main_module.google_oauth.new_nonce()
        main_module.db.create_oauth_state(state, "gmail", alice["id"], verifier, nonce, "https://app.example.workers.dev/auth/gmail/callback")
        same = google_id_token(self.key, sub="gm-ok-sub", nonce=nonce, email="alice@example.com")
        self._patch_google(same, extra={"refresh_token": "super-secret-refresh", "access_token": "at", "expires_in": 3600})
        resp = client.get(f"/auth/gmail/callback?state={state}&code=abc", follow_redirects=False)
        self.assertIn(resp.status_code, _REDIRECTS, resp.text)
        connection = main_module.db.get_gmail_connection(alice["id"])
        self.assertIsNotNone(connection)
        self.assertEqual(connection["status"], "connected")
        self.assertNotIn("super-secret-refresh", str(connection["refresh_token_enc"]))
        # The connection must be decryptable only with the app key.
        from app.auth.crypto import decrypt

        self.assertEqual(decrypt(connection["refresh_token_enc"], main_module.settings.app_encryption_key), "super-secret-refresh")

    def test_session_reports_gmail_status_for_the_owner_only(self):
        alice = main_module.db.upsert_user("gm-report-sub", "alice@example.com", True, "Alice", "")
        client, _ = support.make_client(alice["id"])
        session_resp = client.get("/auth/session").json()
        self.assertFalse(session_resp["gmail"]["connected"])
        main_module.db.save_gmail_connection(
            user_id=alice["id"], google_sub="alice-sub", email="alice@gmail.com",
            scope="openid", refresh_token_enc="x", access_token_enc="y", token_expiry=time.time() + 100,
        )
        session_resp = client.get("/auth/session").json()
        self.assertTrue(session_resp["gmail"]["connected"])
        self.assertEqual(session_resp["gmail"]["email"], "alice@gmail.com")
        # A different user's session shows no connection, not the other mailbox.
        bob = main_module.db.upsert_user("bob-sub", "bob@example.com", True, "Bob", "")
        bob_client, _ = support.make_client(bob["id"])
        session_resp = bob_client.get("/auth/session").json()
        self.assertFalse(session_resp["gmail"]["connected"])


class TestGatewayAssertedIdentity(unittest.TestCase):
    """The edge-asserted identity path, used when the Cloudflare Worker proxies.

    The Worker strips the browser cookie and asserts the verified Google subject
    in headers.  This backend trusts those headers only after checking the
    shared internal token, so the tests below prove both halves: a valid
    assertion authenticates and provisions a user, and the same headers without
    the token reach nothing.
    """

    def setUp(self):
        _configure_google()
        from fastapi.testclient import TestClient

        main_module.settings.rag_internal_token = support.INTERNAL
        self.client = TestClient(main_module.app)
        self.client.headers[support.INTERNAL_HEADER] = support.INTERNAL

    def test_asserted_identity_authenticates_without_a_cookie(self):
        self.client.headers["x-rag-user-sub"] = "edge-sub-1"
        self.client.headers["x-rag-user-email"] = "edge@example.com"
        self.client.headers["x-rag-user-email-verified"] = "1"
        self.client.headers["x-rag-user-name"] = "Edge User"
        resp = self.client.get("/threads")
        self.assertEqual(resp.status_code, 200, resp.text)
        user = main_module.db.get_user_by_sub("edge-sub-1")
        self.assertIsNotNone(user, "the asserted subject must provision a backend user")
        self.assertEqual(user["email"], "edge@example.com")
        self.assertEqual(user["name"], "Edge User")

    def test_state_changing_call_via_gateway_needs_no_backend_csrf(self):
        # The edge enforces CSRF before proxying; the backend sees no cookie and
        # holds no matching token, so demanding one here would break every
        # mutation through the Worker.
        self.client.headers["x-rag-user-sub"] = "edge-sub-2"
        resp = self.client.post("/threads", json={"title": "edge"})
        self.assertEqual(resp.status_code, 200, resp.text)
        user = main_module.db.get_user_by_sub("edge-sub-2")
        self.assertEqual(resp.json()["user_id"], user["id"])

    def test_asserted_identity_is_ignored_without_the_internal_token(self):
        self.client.headers.pop(support.INTERNAL_HEADER, None)
        self.client.headers["x-rag-user-sub"] = "edge-sub-3"
        self.client.headers["x-rag-user-email"] = "attacker@example.com"
        resp = self.client.get("/threads")
        self.assertEqual(resp.status_code, 401)
        self.assertIsNone(
            main_module.db.get_user_by_sub("edge-sub-3"),
            "an untrusted assertion must not provision a user",
        )


class TestLegacyQuarantine(unittest.TestCase):
    def test_legacy_threads_are_quarantined_not_exposed(self):
        import tempfile as _tf

        from app.db import Database

        with _tf.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "rag.db")
            dbase = Database(path)
            try:
                legacy_id = dbase.create_thread("me", "legacy conversation")
                dbase._quarantine_legacy_threads()
                self.assertTrue(dbase.is_quarantined(legacy_id))
                self.assertEqual(dbase.list_threads("anyone"), [])
                self.assertFalse(dbase.thread_owned_by(legacy_id, "someone"))
            finally:
                dbase.conn.close()


if __name__ == "__main__":
    unittest.main()