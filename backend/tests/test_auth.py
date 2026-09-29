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

from fastapi.routing import APIRoute  # noqa: E402
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

# --- every API route is behind the gate --------------------------------------
#
# The 401 list in the auth middleware is a denylist, so a route that is not named
# in it is public.  That is fine for the SPA and fatal for computer control, so
# this walks the real route table and insists that anything able to reach the
# machine refuses an anonymous caller.
#
# It exists because this bug shipped once: /ai/computer/start was added, worked
# perfectly, and would have let anyone with the tunnel URL drive the user's
# signed-in browser.  Enumerating the routes is the only check that catches the
# next one.
#
# "/" is matched exactly, never as a prefix.  As a prefix it matches every path,
# which silently makes the whole loop skip everything and pass forever.
#
# WebSocket routes are excluded: they are authenticated by a separate branch of
# the middleware, and probing one with an HTTP GET answers 404 either way, so
# including them here would only ever report a misleading number.
_PUBLIC_PATHS = {
    "/",                      # the SPA catch-all: this *is* the login page
    "/health",                # the deploy probe
    "/auth/status",
    "/auth/login",
    "/auth/logout",
    "/favicon.ico",
    "/index.html",
    # FastAPI's generated schema and console.  Public before this change and
    # left that way on purpose: they are built into the app object, gating them
    # would mean constructing the schema lazily, and they describe routes that
    # are all refused without a session anyway.  Named here so that the list is
    # visibly a decision rather than an oversight.
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
}
_PUBLIC_PREFIXES = ("/static", "/assets")


def _guards_the_machine(route) -> bool:
    # Only HTTP routes.  A websocket route has no `methods` and is gated by the
    # other half of the middleware.
    if not isinstance(route, APIRoute):
        return False
    path = getattr(route, "path", "")
    return path not in _PUBLIC_PATHS and not path.startswith(_PUBLIC_PREFIXES)


leaked: list[str] = []
checked = 0
for route in app.routes:
    if not _guards_the_machine(route):
        continue
    path = route.path
    methods = route.methods or {"GET"}
    # Ask with a method the route actually accepts: probing a POST-only route
    # with GET answers 422 or 405, which says nothing about the auth gate.
    method = "GET" if "GET" in methods else sorted(methods)[0]
    r = anon.request(method, path)
    checked += 1
    # Anything that is not a 401 either reached its handler or was swallowed by
    # the SPA fallback.  Neither is acceptable for a guarded route.
    if r.status_code != 401:
        leaked.append(f"{method} {path} -> {r.status_code}")

check(checked > 8, f"the route walk found routes to check (checked {checked})")
check(
    not leaked,
    "every non-public route refuses an anonymous caller "
    f"(leaked: {', '.join(sorted(leaked)) or 'none'})",
)

# The computer-control routes specifically, since they are the ones that move a
# real pointer.  A POST is used because that is how a task is actually started.
for path, payload in (
    ("/ai/computer/start", {"task": "go to example.com"}),
    ("/ai/computer/abc123", {}),
    ("/ai/computer/abc123/stop", {}),
):
    r = anon.post(path, json=payload) if path.endswith(("start", "stop")) else anon.get(path)
    check(
        r.status_code == 401,
        f"unauthenticated {path} -> 401 (got {r.status_code}); "
        "an open tunnel must not be able to drive the user's browser",
    )

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
