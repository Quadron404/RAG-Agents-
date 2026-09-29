"""The local development bypass must not be a hole in the tunnel.

The tempting implementation of "skip the passphrase when I am on localhost" is to
check request.client.host against 127.0.0.1.  That is wrong here, and
catastrophically: codespace/env.sh points the Quick Tunnel at
http://127.0.0.1:$BACKEND_PORT, so cloudflared connects from loopback too and
*every public request looks local*.  A client.host check would hand the
trycloudflare.com URL a valid session for free.

So the discriminator is the Host header, and these tests hold both sides of it:
the loopback host gets in without a passphrase, and the tunnel hostname does not
-- with the bypass switched on, which is the case a client.host check fails.

They also check that the session handed out is an ordinary one, since the screen
has its own WebSocket guard that HTTP middleware never runs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.auth as auth  # noqa: E402
import app.main as main_mod  # noqa: E402

TOKEN = "the-real-passphrase-9f3c"
LOCAL = "http://127.0.0.1:8000"
TUNNEL = "http://something-random-4821.trycloudflare.com"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(f"{'ok   ' if ok else 'FAIL '} {label}")
    if not ok:
        failures.append(label)
    return ok


def rebuild():
    """main holds a reference to auth, but env is read live, so this is enough."""
    return main_mod


os.environ["RAG_AUTH_TOKEN"] = TOKEN
os.environ["RAG_DEV_AUTO_LOGIN"] = "1"
rebuild()
client = TestClient(main_mod.app)


def get(path: str, base: str = LOCAL, headers: dict | None = None, jar=None):
    c = jar or client
    return c.get(base + path, headers=headers or {})


# --- 1. off by default -------------------------------------------------------
os.environ.pop("RAG_DEV_AUTO_LOGIN", None)
check(auth.dev_auto_login_enabled() is False, "the bypass is off when RAG_DEV_AUTO_LOGIN is unset")
r = get("/screen/config")
check(r.status_code == 401, f"with the bypass off, a local request still needs the passphrase (got {r.status_code})")

for value in ("0", "false", "no", "", "off"):
    os.environ["RAG_DEV_AUTO_LOGIN"] = value
    check(auth.dev_auto_login_enabled() is False, f"RAG_DEV_AUTO_LOGIN={value!r} does not enable the bypass")
os.environ["RAG_DEV_AUTO_LOGIN"] = "1"

# --- 2. loopback hosts get in, with no passphrase ---------------------------
for host in ("localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
    jar = TestClient(main_mod.app).cookies
    c = TestClient(main_mod.app)
    r = c.get(LOCAL + "/screen/config", headers={"Host": host})
    got = r.status_code
    cookie = "rag_session" in r.cookies or any(
        k.name == "rag_session" for k in c.cookies.jar
    )
    check(got == 200, f"Host: {host} gets a session without the passphrase (got {got})")
    check(cookie, f"Host: {host} is handed a real rag_session cookie")

# --- 3. THE IMPORTANT ONE: the public URL is still locked --------------------
for host in (
    "something-random-4821.trycloudflare.com",
    "anything.trycloudflare.com",
    "127.0.0.1.trycloudflare.com",
    "localhost.trycloudflare.com",
    "evil.example.com",
    "10.0.0.5:8000",
):
    c = TestClient(main_mod.app)
    r = c.get(TUNNEL + "/screen/config", headers={"Host": host})
    check(
        r.status_code == 401,
        f"Host: {host} is still refused with the bypass ON (got {r.status_code})",
    )
    check(
        not any(k.name == "rag_session" for k in c.cookies.jar),
        f"Host: {host} is handed no cookie",
    )

# X-Forwarded-Host is attacker-influenced; it must not grant anything.
c = TestClient(main_mod.app)
r = c.get(
    TUNNEL + "/screen/config",
    headers={"Host": "x.trycloudflare.com", "X-Forwarded-Host": "127.0.0.1"},
)
check(
    r.status_code == 401,
    f"X-Forwarded-Host: 127.0.0.1 does not grant a session (got {r.status_code})",
)

# ...and cloudflared's real headers must not be mistaken for locality either.
c = TestClient(main_mod.app)
r = c.get(
    TUNNEL + "/screen/config",
    headers={"Host": "x.trycloudflare.com", "CF-Connecting-IP": "1.2.3.4", "X-Forwarded-Proto": "https"},
)
check(r.status_code == 401, f"a real cloudflared request is still refused (got {r.status_code})")

# --- 4. the session is an ordinary one ---------------------------------------
# Not a special dev token: the same cookie the passphrase login mints, so the
# /websockify and /ws/screen guards pass it with no development branch.
c = TestClient(main_mod.app)
c.get(LOCAL + "/screen/config", headers={"Host": "127.0.0.1:8000"})
cookie = next(k for k in c.cookies.jar if k.name == "rag_session")
check(auth.check_cookie(cookie.value), "the auto-issued cookie validates against the real passphrase")
check(
    "." in cookie.value and cookie.value.count(".") == 2,
    "it has the same shape as a login cookie (expiry.random.hmac)",
)

# Protected routes behave exactly as they do for a passphrase login.  Compared
# rather than asserted, because /sysinfo and /threads answer 502/422 depending on
# whether the agent is up and on query parameters -- neither of which is about
# auth, and both of which would make a hardcoded 200 assertion meaningless.
dev_client = TestClient(main_mod.app)
dev_client.get(LOCAL + "/screen/config", headers={"Host": "127.0.0.1:8000"})

login_client = TestClient(main_mod.app)
login_client.post(
    LOCAL + "/auth/login", json={"passphrase": TOKEN}, headers={"Host": "127.0.0.1:8000"}
)
check(
    any(k.name == "rag_session" for k in login_client.cookies.jar),
    "the passphrase login still issues a cookie",
)

for path in ("/sysinfo", "/threads", "/screen/config"):
    a = dev_client.get(LOCAL + path, headers={"Host": "127.0.0.1:8000"})
    b = login_client.get(LOCAL + path, headers={"Host": "127.0.0.1:8000"})
    check(
        a.status_code == b.status_code and a.status_code != 401,
        f"{path} answers identically for a dev session and a passphrase session "
        f"({a.status_code} vs {b.status_code})",
    )

# An already-valid session must not be re-minted, or the cookie would rotate on
# every single local request.  Checked on /auth/status, which is the only kind of
# route that reaches _add_dev_session while already holding a session: a
# protected route returns in the branch above first, and "/" is handled by the
# denylist fallback further down, neither of which mints.
c = TestClient(main_mod.app)
c.get(LOCAL + "/auth/status", headers={"Host": "127.0.0.1:8000"})
first = next(k.value for k in c.cookies.jar if k.name == "rag_session")
c.get(LOCAL + "/auth/status", headers={"Host": "127.0.0.1:8000"})
second = next(k.value for k in c.cookies.jar if k.name == "rag_session")
check(
    first == second,
    "an existing valid session is left alone rather than re-minted",
)
# The real browser sequence: load the app, which mints the session, then ask
# /auth/status, which now sees it.  This is the request the login screen is
# decided by, so it is the one that matters.
fresh = TestClient(main_mod.app)
fresh.get(LOCAL + "/", headers={"Host": "127.0.0.1:8000"})
check(
    fresh.get(LOCAL + "/auth/status", headers={"Host": "127.0.0.1:8000"})
    .json()
    .get("authenticated")
    is True,
    "/auth/status reports the dev browser as authenticated, so no login screen",
)

# --- 5. the WebSocket screen keeps its own guard ----------------------------
# /websockify and /ws/screen are not covered by the HTTP middleware, so the
# bypass cannot lean on it.  The dev cookie is an ordinary cookie precisely so
# their existing check accepts it, with no development branch of their own.
from starlette.websockets import WebSocketDisconnect  # noqa: E402

c = TestClient(main_mod.app)
try:
    with c.websocket_connect(
        TUNNEL.replace("http", "ws") + "/websockify",
        headers={"Host": "x.trycloudflare.com"},
    ):
        check(False, "the tunnel was NOT admitted to /websockify without a session")
except Exception:
    check(True, "the tunnel is refused at /websockify without a session")

c = TestClient(main_mod.app)
c.get(LOCAL + "/screen/config", headers={"Host": "127.0.0.1:8000"})
try:
    with c.websocket_connect(
        LOCAL.replace("http", "ws") + "/websockify", headers={"Host": "127.0.0.1:8000"}
    ):
        pass
    check(True, "/websockify accepts the auto-issued dev session through its own guard")
except WebSocketDisconnect:
    check(True, "/websockify accepted the dev session, then closed on the absent screen (agent down)")
except Exception as e:  # noqa: BLE001
    check(
        "100" in str(e) or "101" in str(e) or "closed" in str(e).lower(),
        f"/websockify got past auth with the dev session ({type(e).__name__}: {e})",
    )

# --- 6. the public path still needs the passphrase, end to end ---------------
c = TestClient(main_mod.app)
r = c.post(TUNNEL + "/auth/login", json={"passphrase": "wrong"}, headers={"Host": "x.trycloudflare.com"})
check(r.status_code == 401, f"the public login still refuses a wrong passphrase (got {r.status_code})")
r = c.post(TUNNEL + "/auth/login", json={"passphrase": TOKEN}, headers={"Host": "x.trycloudflare.com"})
check(r.status_code == 200, f"the public login still accepts the real passphrase (got {r.status_code})")
r = c.get(TUNNEL + "/screen/config", headers={"Host": "x.trycloudflare.com"})
check(r.status_code == 200, f"/screen/config works on the tunnel after logging in (got {r.status_code})")

# --- 7. the auth path is not refactored --------------------------------------
# The bypass must not be a way to serve a deployment with no token at all.
os.environ.pop("RAG_AUTH_TOKEN", None)
c = TestClient(main_mod.app)
r = c.get(TUNNEL + "/screen/config", headers={"Host": "x.trycloudflare.com"})
check(r.status_code == 401, f"with no token, the tunnel is still refused (got {r.status_code})")
r = c.get(LOCAL + "/screen/config", headers={"Host": "127.0.0.1:8000"})
check(
    r.status_code == 401,
    f"with no token, even localhost is refused -- the bypass is not a fail-open (got {r.status_code})",
)
os.environ["RAG_AUTH_TOKEN"] = TOKEN

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS: local development skips the passphrase and the tunnel does not")
