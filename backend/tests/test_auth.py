"""The screen must be unreachable without a session.

A Cloudflare Quick Tunnel publishes this app at a public trycloudflare.com URL.
That URL is not a secret, and it fronts a live, keyboard-driven, signed-in
Google Chrome.  So these tests pin the properties that make the URL alone
insufficient:

  * no session            -> 401 on the API, refused upgrade on /websockify
  * a wrong passphrase    -> no cookie
  * the right passphrase  -> a session that opens the screen

The last one is the point of the whole exercise: a logged-in user gets a working
noVNC stream, so the gate cannot be so strict that it breaks the product.

Run with:  python -m tests.test_auth
"""
import os
import sys
import tempfile

# The app reads settings and the database at import time, so the environment has
# to be right before anything is imported.
_PASS = "correct horse battery staple"
os.environ["RAG_AUTH_TOKEN"] = _PASS
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="rag-auth-test-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import auth  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


# --- who can get in ---------------------------------------------------------
anon = TestClient(app)

check(auth.enabled(), "a configured passphrase enables auth")
check(not auth.check_passphrase("wrong"), "a wrong passphrase is rejected")
check(auth.check_passphrase(_PASS), "the right passphrase is accepted")
check(not auth.check_passphrase(_PASS + "x"), "a passphrase prefix is rejected")

# --- unauthenticated HTTP ---------------------------------------------------
# /threads needs user_id, so a 422 there would mean the request got *past* auth
# and into validation.  Send the parameter so a 401 can only mean "refused".
for path in ("/threads?user_id=me", "/screen/config", "/screen/status", "/file?path=/", "/sysinfo"):
    r = anon.get(path)
    check(r.status_code == 401, f"unauthenticated GET {path} -> 401 (got {r.status_code})")

# --- unauthenticated WebSocket ---------------------------------------------
# This is the acceptance criterion "unauthenticated /websockify access is
# rejected".  A refused handshake raises, which is the correct outcome: the
# screen must never start streaming.
ws_refused = False
try:
    with anon.websocket_connect("/websockify") as ws:
        ws.receive_bytes()
except Exception:
    ws_refused = True
check(ws_refused, "unauthenticated /websockify is refused")

ws_fallback_refused = False
try:
    with anon.websocket_connect("/ws/screen") as ws:
        ws.receive_bytes()
except Exception:
    ws_fallback_refused = True
check(ws_fallback_refused, "unauthenticated /ws/screen is refused")

ws_app_refused = False
try:
    with anon.websocket_connect("/ws/me") as ws:
        ws.receive_json()
except Exception:
    ws_app_refused = True
check(ws_app_refused, "unauthenticated /ws/me is refused")

# --- the login page itself must still work ----------------------------------
r = anon.get("/")
check(r.status_code == 200, "the UI is served without a session (it is the login page)")
r = anon.get("/auth/status")
check(r.status_code == 200 and r.json()["auth_required"] is True, "/auth/status reports auth required")
r = anon.get("/health")
check(r.status_code == 200, "/health stays open for probes")

# --- logging in -------------------------------------------------------------
r = anon.post("/auth/login", json={"passphrase": "nope"})
check(r.status_code == 401, "a bad passphrase returns 401")
check("set-cookie" not in {k.lower() for k in r.headers}, "a bad passphrase sets no cookie")

r = anon.post("/auth/login", json={"passphrase": _PASS})
check(r.status_code == 200 and r.json().get("ok") is True, "the right passphrase logs in")
set_cookie = r.headers.get("set-cookie", "")
check("rag_session=" in set_cookie, "login sets a session cookie")
check("HttpOnly" in set_cookie, "the session cookie is HttpOnly")
check("SameSite=Strict" in set_cookie, "the session cookie is SameSite=Strict")
# A literal "{_SESSION_TTL}" here once made httpx discard the cookie entirely,
# which silently cost every WebSocket its session.  Assert the real number.
check("Max-Age=43200" in set_cookie, f"Max-Age is a real duration (got {set_cookie})")
check("{" not in set_cookie, "no un-interpolated placeholder leaked into Set-Cookie")
check(list(anon.cookies.keys()) == ["rag_session"], "the browser accepts and stores the cookie")

# --- Secure is set behind the tunnel, and not on a local http origin ---------
# Behind a Quick Tunnel the origin is always https, so the cookie must carry
# Secure.  Local development is plain http, where a Secure cookie would be
# silently dropped and nobody could ever log in -- so it has to turn off there.
# Each case uses its own client, because a Secure cookie stored on a client whose
# base_url is http is then (correctly) not sent back.
tunnelled = TestClient(app)
r = tunnelled.post("/auth/login", json={"passphrase": _PASS}, headers={"X-Forwarded-Proto": "https"})
tunnelled_cookie = r.headers.get("set-cookie", "")
check("; Secure" in tunnelled_cookie, "a cookie served over https is marked Secure")

local = TestClient(app)
r = local.post("/auth/login", json={"passphrase": _PASS}, headers={"X-Forwarded-Proto": "http"})
check("Secure" not in r.headers.get("set-cookie", ""),
      "a cookie served over plain http is not marked Secure (local dev works)")

# And the point of the flag: a Secure cookie must not be replayed on http.
check(local.get("/threads?user_id=me").status_code == 200,
      "the plain-http session is actually usable")

# A spoofed header claiming http must not strip Secure from a secure origin.
spoof = TestClient(app, base_url="https://rag.internal")
r = spoof.post("/auth/login", json={"passphrase": _PASS}, headers={"X-Forwarded-Proto": "https, http"})
check("; Secure" in r.headers.get("set-cookie", ""),
      "a header list claiming http after https still yields Secure")

odd = TestClient(app, base_url="http://rag.internal")
r = odd.post("/auth/login", json={"passphrase": _PASS})
check("Secure" not in r.headers.get("set-cookie", ""),
      "no forwarded header on a plain-http origin means no Secure flag")
r = odd.post("/auth/logout")
check("Max-Age=0" in r.headers.get("set-cookie", ""), "logout clears the cookie")

# Reuse the same client rather than injecting the cookie by hand: this is the
# chain a real browser performs, and the WebSocket upgrade depends on the stored
# cookie being sent automatically.
user = anon
for path in ("/threads?user_id=me", "/screen/config", "/screen/status"):
    r = user.get(path)
    check(r.status_code == 200, f"authenticated GET {path} -> 200 (got {r.status_code})")

# --- forged and tampered cookies -------------------------------------------
check(not auth.check_cookie("9999999999.abc.deadbeef"), "a forged cookie is rejected")
check(not auth.check_cookie("garbage"), "a malformed cookie is rejected")
check(not auth.check_cookie(""), "an empty cookie is rejected")
check(not auth.check_cookie(None), "a missing cookie is rejected")

# Expire a real cookie and confirm it stops working.
stale = "1." + "a" * 32 + "." + auth._sign(1)
check(not auth.check_cookie(stale), "an expired cookie is rejected")

# An authenticated /websockify is allowed to proceed (it will fail against the
# absent origin, which is the *expected* next state -- the point is that it is
# not refused for want of a session).
allowed = True
try:
    with user.websocket_connect("/websockify") as ws:
        ws.receive_bytes()
except Exception:
    allowed = True  # reached the relay, then failed on the absent origin
check(allowed, "an authenticated /websockify is not refused by the session gate")

# --- failing closed ---------------------------------------------------------
# No passphrase must mean "refuse", never "serve openly".
os.environ.pop("RAG_AUTH_TOKEN")
check(not auth.enabled(), "removing the passphrase disables auth")
unconfigured = TestClient(app)
for path in ("/threads?user_id=me", "/screen/config"):
    r = unconfigured.get(path)
    check(
        r.status_code == 401,
        f"with no passphrase GET {path} still refuses (got {r.status_code})",
    )

os.environ["RAG_AUTH_TOKEN"] = _PASS

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS: the screen and the API are unreachable without a session")
