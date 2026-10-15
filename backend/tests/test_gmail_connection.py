"""The Gmail connection: separate consent, real status, and safe token life.

The properties proven here, each one a way this could go wrong:

* login and Gmail are separate grants -- login asks for no Gmail scope, and only
  ``gmail.send`` + ``gmail.readonly`` are requested on top for the mailbox;
* the mailbox is bound to the signed-in Google identity (the mismatch case is in
  ``test_auth``; the scope separation is asserted here);
* the connection state reported to the browser is the backend's real state, and
  the refresh/access tokens never appear in a response;
* an expired access token is refreshed, and the new one is stored encrypted;
* a revoked grant (``invalid_grant``) is *permanent*: the connection is marked
  and no further refresh is attempted -- it cannot be retried forever;
* a transient refresh failure is retryable and does not destroy the connection;
* disconnect removes the record and asks Google to revoke the grant.

The Google token endpoint is stubbed: these are the connection's own rules, and
the live exchange is exercised by ``test_auth`` against a real request shape.
"""
import asyncio
import os
import sys
import time

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import tests.support as support  # noqa: E402  (configures DATA_DIR before app.main)

import app.main as main_module  # noqa: E402
from app.auth import google as google_oauth  # noqa: E402
from app.email import gmail as gmail_connection  # noqa: E402
from app.email.gmail import GmailError  # noqa: E402

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


db = main_module.db
auth = main_module.auth
settings = main_module.settings
settings.google_client_id = "client-id"
settings.google_client_secret = "client-secret"
settings.public_base_url = "https://app.example.workers.dev"

GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"


# --- the scopes are exactly the intended ones -------------------------------
full = set(google_oauth.GMAIL_FULL_SCOPES)
check("the mailbox grant asks to send", GMAIL_SEND in full)
check("the mailbox grant asks to read (read-only)", GMAIL_READONLY in full)
check("the mailbox grant keeps the OIDC scopes",
      {"openid", "email", "profile"} <= full, sorted(full))
check("the Gmail scopes are exactly send + readonly",
      set(google_oauth.GMAIL_SCOPES) == {GMAIL_SEND, GMAIL_READONLY}, sorted(google_oauth.GMAIL_SCOPES))
for forbidden in (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/drive",
):
    check(f"no unrelated scope: {forbidden.rsplit('/', 1)[-1]}", forbidden not in full)
check("login requests no Gmail scope",
      not any("gmail" in s for s in google_oauth.scopes_for("login")))
check("login does not request offline access",
      "access_type=online" in google_oauth.build_authorization_url(
          settings=settings, redirect_uri="https://x/cb", state="s", nonce="n",
          verifier=google_oauth.new_code_verifier(), mode="login"))


# --- the connect URL reconnects with consent --------------------------------
redirect_url = google_oauth.build_authorization_url(
    settings=settings, redirect_uri="https://x/cb", state="s", nonce="n",
    verifier=google_oauth.new_code_verifier(), mode="gmail", login_hint="me@example.com")
check("the mailbox grant re-prompts for consent", "prompt=consent" in redirect_url, redirect_url)
check("the mailbox grant requests offline access", "access_type=offline" in redirect_url)
check("the connect URL hints the signed-in mailbox", "login_hint=me%40example.com" in redirect_url)


# --- failure classification --------------------------------------------------
check("invalid_grant is a permanent error",
      "invalid_grant" in google_oauth.PERMANENT_ERROR_CODES)
perm = google_oauth._oauth_error_from_body(400, {"error": "invalid_grant"}, "refresh failed")
check("a 400 invalid_grant is marked non-retryable", perm.retryable is False and perm.error_code == "invalid_grant")
trans = google_oauth._oauth_error_from_body(503, {"error": "server_error"}, "refresh failed")
check("a 503 is marked retryable", trans.retryable is True)


# --- helpers ----------------------------------------------------------------
def save_connection(user_id, *, refresh, access, expiry):
    db.save_gmail_connection(
        user_id=user_id,
        google_sub=f"{user_id}-sub",
        email=f"{user_id}@gmail.com",
        scope=" ".join(google_oauth.GMAIL_FULL_SCOPES),
        refresh_token_enc=auth.encrypt_secret(refresh),
        access_token_enc=auth.encrypt_secret(access),
        token_expiry=expiry,
    )


def patch_refresh(fn):
    main_module.google_oauth.refresh_access_token = fn


def patch_revoke(fn):
    main_module.google_oauth.revoke_token = fn


# --- status is honest and per-user ------------------------------------------
check("an unknown user has no connection", gmail_connection.status_for(db, "nobody")["status"] == "disconnected")
save_connection("u-status", refresh="r", access="a", expiry=time.time() + 3600)
summary = gmail_connection.status_for(db, "u-status")
check("a connected mailbox reports connected + address",
      summary["connected"] and summary["email"] == "u-status@gmail.com", summary)
db.set_gmail_status("u-status", "revoked", "reconnect")
summary = gmail_connection.status_for(db, "u-status")
check("a revoked mailbox is not reported connected", summary["connected"] is False)
check("a revoked mailbox asks for reconnect", summary["needsReconnect"] is True)
check("the status never carries a token",
      "token" not in str(summary).lower() and "refresh" not in str(summary).lower(), summary)


# --- a valid cached access token is used as-is ------------------------------
save_connection("u-cached", refresh="r", access="cached-access", expiry=time.time() + 3600)


async def must_not_refresh(**_kwargs):
    raise AssertionError("refresh must not be called while the access token is valid")


patch_refresh(must_not_refresh)
token = asyncio.run(gmail_connection.access_token(db, auth, settings, "u-cached"))
check("a valid access token is returned without refreshing", token == "cached-access", token)


# --- an expired access token is refreshed and stored encrypted --------------
save_connection("u-refresh", refresh="the-refresh", access="stale-access", expiry=time.time() - 10)


async def refresh_ok(**_kwargs):
    return {"access_token": "fresh-access", "expires_in": 3600}


patch_refresh(refresh_ok)
token = asyncio.run(gmail_connection.access_token(db, auth, settings, "u-refresh"))
check("an expired access token is refreshed", token == "fresh-access", token)
row = db.get_gmail_connection("u-refresh")
check("the refreshed mailbox is connected again", row["status"] == "connected", row["status"])
check("the refreshed access token is stored encrypted",
      auth.decrypt_secret(str(row["access_token_enc"])) == "fresh-access" and "fresh-access" not in str(row["access_token_enc"]))
check("the refresh token is preserved",
      auth.decrypt_secret(str(row["refresh_token_enc"])) == "the-refresh")
check("the expiry moved forward", float(row["token_expiry"]) > time.time() + 3000)


# --- a revoked grant is permanent and is not retried ------------------------
save_connection("u-revoked", refresh="r", access="stale", expiry=time.time() - 10)
refresh_calls = {"n": 0}


async def refresh_revoked(**_kwargs):
    refresh_calls["n"] += 1
    raise google_oauth.OAuthError("refresh failed (invalid_grant)", error_code="invalid_grant",
                                  status=400, retryable=False)


patch_refresh(refresh_revoked)
try:
    asyncio.run(gmail_connection.access_token(db, auth, settings, "u-revoked"))
    check("a revoked grant raises rather than returning a token", False)
except GmailError as exc:
    check("a revoked grant is not retryable", exc.retryable is False, exc)
    check("a revoked grant asks the user to reconnect", exc.needs_reconnect is True)
row = db.get_gmail_connection("u-revoked")
check("a revoked grant is recorded as revoked", row["status"] == "revoked", row["status"])

# The second call must not reach the token endpoint at all.
async def refresh_must_not_run(**_kwargs):
    raise AssertionError("a revoked connection must not be refreshed again")


patch_refresh(refresh_must_not_run)
try:
    asyncio.run(gmail_connection.access_token(db, auth, settings, "u-revoked"))
except GmailError:
    pass
check("a revoked connection is not retried indefinitely", refresh_calls["n"] == 1, refresh_calls)


# --- a transient failure is retryable and keeps the connection --------------
save_connection("u-transient", refresh="keep-me", access="stale", expiry=time.time() - 10)


async def refresh_flaky(**_kwargs):
    raise google_oauth.OAuthError("refresh failed (server_error)", error_code="server_error",
                                  status=503, retryable=True)


patch_refresh(refresh_flaky)
try:
    asyncio.run(gmail_connection.access_token(db, auth, settings, "u-transient"))
    check("a transient failure raises", False)
except GmailError as exc:
    check("a transient failure is retryable", exc.retryable is True and exc.needs_reconnect is False, exc)
row = db.get_gmail_connection("u-transient")
check("a transient failure keeps the connection row", row is not None)
check("a transient failure does not mark the connection revoked", row["status"] == "error", row["status"])

patch_refresh(refresh_ok)
token = asyncio.run(gmail_connection.access_token(db, auth, settings, "u-transient"))
check("a retry after a transient failure succeeds", token == "fresh-access", token)
check("the connection recovers to connected",
      db.get_gmail_connection("u-transient")["status"] == "connected")


# --- no token leaks through the HTTP surface --------------------------------
alice = db.upsert_user("gmail-leak-sub", "leak@example.com", True, "Leak", "")
save_connection("__unused__", refresh="r", access="a", expiry=time.time() + 100)
db.delete_gmail_connection("__unused__")
db.save_gmail_connection(
    user_id=alice["id"],
    google_sub="gmail-leak-sub",
    email="leak@gmail.com",
    scope=" ".join(google_oauth.GMAIL_FULL_SCOPES),
    refresh_token_enc=auth.encrypt_secret("REFRESH-SECRET-XYZ"),
    access_token_enc=auth.encrypt_secret("ACCESS-SECRET-XYZ"),
    token_expiry=time.time() + 3600,
)
client, _ = support.make_client(alice["id"])
for path in ("/auth/gmail/status", "/auth/session"):
    body = client.get(path).text
    check(f"{path} never returns the refresh token", "REFRESH-SECRET-XYZ" not in body, path)
    check(f"{path} never returns the access token", "ACCESS-SECRET-XYZ" not in body, path)
    check(f"{path} never returns the encrypted column", "token_enc" not in body, path)
status_body = client.get("/auth/gmail/status").json()
check("the status endpoint reflects the real mailbox", status_body["connected"] and status_body["email"] == "leak@gmail.com", status_body)

# A caller without a session cannot read anyone's status.
no_session = support.raw_client()
check("gmail status requires a session", no_session.get("/auth/gmail/status").status_code == 401)


# --- consent failure is reported, not swallowed -----------------------------
client2, _ = support.make_client(db.upsert_user("gmail-denied-sub", "d@example.com", True, "D", "")["id"])
denied = client2.get("/auth/gmail/callback?error=access_denied", follow_redirects=False)
check("a denied consent redirects with a flag", denied.headers.get("location", "").endswith("/?gmail=denied"),
      denied.headers.get("location"))


# --- disconnect revokes at Google and forgets locally -----------------------
user = db.upsert_user("gmail-disc-sub", "disc@example.com", True, "Disc", "")
save_connection(user["id"], refresh="disc-refresh", access="a", expiry=time.time() + 100)
seen = {"token": ""}


async def revoke_ok(*, settings, token, client=None):
    seen["token"] = token
    return True


patch_revoke(revoke_ok)
disc_client, _ = support.make_client(user["id"])
resp = disc_client.post("/auth/gmail/disconnect").json()
check("disconnect reports success", resp.get("ok") is True)
check("disconnect asks Google to revoke the real refresh token", seen["token"] == "disc-refresh", seen["token"])
check("disconnect removes the local connection", db.get_gmail_connection(user["id"]) is None)
check("disconnect reports the mailbox disconnected", resp["gmail"]["connected"] is False, resp["gmail"])


# --- a failed revocation still disconnects locally --------------------------
user2 = db.upsert_user("gmail-disc2-sub", "disc2@example.com", True, "Disc2", "")
save_connection(user2["id"], refresh="r", access="a", expiry=time.time() + 100)


async def revoke_boom(*, settings, token, client=None):
    raise RuntimeError("network down")


patch_revoke(revoke_boom)
c2, _ = support.make_client(user2["id"])
resp2 = c2.post("/auth/gmail/disconnect").json()
check("a failed revocation still disconnects locally", resp2.get("ok") is True and db.get_gmail_connection(user2["id"]) is None)

print()
if failures:
    print(f"{len(failures)} gmail-connection check(s) failed")
    sys.exit(1)
print("all gmail-connection checks pass")
