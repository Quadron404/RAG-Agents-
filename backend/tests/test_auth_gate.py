"""The sign-in gate must not fail open.

A Cloudflare Quick Tunnel puts this app on the open internet.  The backend
refuses every route when no passphrase is configured, which is correct, and
``test_auth`` pins that.  This file pins the other half of the same contract,
which is what actually broke in production:

    no passphrase  ->  /auth/status says auth_required: false
                   ->  the UI must show a configuration error
                   ->  it must NOT show the app

The UI used to treat ``auth_required: false`` as "no login needed here" and
render the workspace.  Every call in that workspace then came back
``{"error": "authentication required"}``, so a missing environment variable
presented as a Computer view that silently refused to draw anything.  Whoever
hit it had to read the backend to learn that the deployment was never finished.

The second half of the file covers the property that made the bug survive a
"19 passed, 0 failed" verification run: the login checks used to be *skipped*
whenever the passphrase was not exported into the shell running the script,
which is the normal case when the token lives in ``backend/.env``.

Run with:  python -m tests.test_auth_gate
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# Settings and the database are read at import time, so the environment has to be
# right before anything is imported.
_PASS = "correct horse battery staple"
os.environ["RAG_AUTH_TOKEN"] = _PASS
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="rag-gate-test-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import auth  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "Frontend" / "src" / "components" / "LoginScreen.tsx"
VERIFY = REPO_ROOT / "codespace" / "verify.sh"

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- the server's half: what /auth/status tells the UI -----------------------
client = TestClient(app)

r = client.get("/auth/status")
body = r.json()
check(r.status_code == 200, "/auth/status answers 200")
check(body["auth_required"] is True, "a configured passphrase is reported as required")
check(body["authenticated"] is False, "an anonymous caller is not reported as authenticated")

# Now the state that caused the outage.
os.environ.pop("RAG_AUTH_TOKEN", None)
check(auth.enabled() is False, "with no passphrase the server reports auth disabled")

unconfigured = TestClient(app)
r = unconfigured.get("/auth/status")
body = r.json()
check(
    body["auth_required"] is False,
    "with no passphrase /auth/status says auth_required: false (this is the state that misled the UI)",
)
# The server's response to it must stay "refuse", for both HTTP and the socket.
for path in ("/threads?user_id=me", "/screen/config", "/sysinfo"):
    r = unconfigured.get(path)
    check(r.status_code == 401, f"with no passphrase GET {path} still refuses (got {r.status_code})")

ws_refused = False
try:
    with unconfigured.websocket_connect("/websockify") as ws:
        ws.receive_bytes()
except Exception:
    ws_refused = True
check(ws_refused, "with no passphrase /websockify is still refused")

os.environ["RAG_AUTH_TOKEN"] = _PASS

# --- the UI's half: it must not render the app in that state -----------------
#
# This is a source-level assertion rather than a browser test on purpose: the
# project has no JS test runner, and the decision is a three-line conditional
# whose entire job is to be readable.  Asserting on the source keeps the rule
# ("auth_required false must not mean open") from being quietly reverted during
# a refactor of the gate.
src = _read(UI)
check(bool(src), "LoginScreen.tsx was found")

if src:
    check("NoPassphraseScreen" in src, "the gate has a dedicated no-passphrase screen")
    check(
        re.search(r"if\s*\(\s*!required\s*\)\s*return\s*<\s*NoPassphraseScreen", src) is not None,
        "the gate returns that screen when auth_required is false",
    )
    # The specific regression: rendering children in that state.  `required` must
    # gate every path to the workspace.
    check(
        re.search(r"if\s*\(\s*required\s*&&\s*!authenticated\s*\)\s*return", src) is None,
        "the workspace is no longer reached via a 'required &&' condition (that skipped the unconfigured case)",
    )
    check(
        re.search(r"if\s*\(\s*!authenticated\s*\)\s*return\s*<\s*LoginScreen", src) is not None,
        "an unauthenticated caller always gets the login screen",
    )
    check(
        "RAG_AUTH_TOKEN" in src,
        "the no-passphrase screen names the variable that has to be set",
    )
    # The passphrase must not be readable by the page: the screen may name the
    # variable, never its value.
    check(_PASS not in src, "the passphrase value appears nowhere in the UI source")

# --- and the verifier must not skip the check that catches it ---------------
verify = _read(VERIFY)
check(bool(verify), "codespace/verify.sh was found")

if verify:
    check("AUTH_TOKEN=" in verify, "verify.sh resolves the passphrase into its own variable")
    check(
        'grep -s \'^RAG_AUTH_TOKEN=\' "$REPO_ROOT/backend/.env"' in verify,
        "verify.sh falls back to backend/.env, where the token normally lives",
    )
    # The app takes the FIRST occurrence of the key (config.py only fills a name
    # that is not already set), so a verifier reading the last one would test a
    # different passphrase than the server is running -- and report a bogus
    # "the passphrase was rejected".
    check(
        re.search(r"head -n1 \| cut -d=", verify) is not None,
        "verify.sh reads the same occurrence of the key that the app does",
    )
    check(
        re.search(r"grep -s '\^RAG_AUTH_TOKEN=' \"\$REPO_ROOT/backend/\.env\" \| tail", verify) is None,
        "verify.sh does not read a later duplicate key than the app",
    )
    # The regression proper: gating the login section on the *shell* variable.
    check(
        '[ -z "$PUBLIC_URL" ] || [ -z "${RAG_AUTH_TOKEN:-}" ]' not in verify,
        "verify.sh no longer skips login when the shell lacks RAG_AUTH_TOKEN",
    )
    # The branch taken when a public tunnel has no readable passphrase must fail.
    # Match the whole elif body so comments inside it do not defeat the check.
    branch = re.search(
        r'elif \[ -z "\$AUTH_TOKEN" \]; then(.*?)\n\s*else\n', verify, re.S
    )
    check(
        branch is not None and re.search(r"^\s*bad ", branch.group(1), re.M) is not None,
        "a public tunnel with no readable passphrase is a FAILURE, not a skip",
    )
    check(
        '"auth_required":true' in verify,
        "verify.sh asks the server whether a passphrase is configured",
    )
    # A skip on a public deployment is what let this reach a user as "all green".
    check(
        'skip "cannot log in without both a tunnel URL and the passphrase"' not in verify,
        "the old 'cannot log in' skip is gone",
    )
    check("exit 1" in verify, "verify.sh still exits non-zero on failure")

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS: a missing passphrase is refused, surfaced, and no longer skips verification")
