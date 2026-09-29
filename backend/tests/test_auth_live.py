"""End-to-end login against a real uvicorn process over real HTTP.

TestClient exercises the ASGI app, which is most of the stack, but the reported
failure involved *how the process was started* -- which parser won, what the
environment held.  Those are properties of a real process, so this starts one the
way boot.sh does and talks to it with a real HTTP client and a real cookie jar.

It also reproduces the exact reported situation: a backend/.env that was
appended to twice, and an interactive shell where RAG_AUTH_TOKEN is unset.  In
that state, before the fix, the server compared against the *last* line.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
PY = sys.executable

FIRST = "the-line-you-can-see"
SECOND = "the-line-append-added-after-it"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(f"{'ok   ' if ok else 'FAIL '} {label}")
    if not ok:
        failures.append(label)
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# A backend tree whose .env has the shape that used to break: the visible line,
# then an appended one.  Exactly what `printf ... >> backend/.env` twice leaves.
d = Path(tempfile.mkdtemp())
backend = d / "backend"
backend.mkdir()
shutil.copytree(APP, backend / "app")
(backend / ".env").write_text(
    "OPENROUTER_API_KEY=\nCOMPUTER_PROVIDER=openrouter\n"
    f"RAG_AUTH_TOKEN={FIRST}\nRAG_AUTH_TOKEN={SECOND}\n",
    encoding="utf-8",
)

port = free_port()
# Started with NO RAG_AUTH_TOKEN in the environment, the way an operator's shell
# looks after they only edited backend/.env.
env = {k: v for k, v in os.environ.items() if k != "RAG_AUTH_TOKEN"}
env["PYTHONWARNINGS"] = "ignore"

proc = subprocess.Popen(
    [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
    cwd=str(backend), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)

base = f"http://127.0.0.1:{port}"
jar = http.cookiejar.CookieJar()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(jar), NoRedirect
)


def call(path: str, data: dict | None = None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        base + path, data=body, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


try:
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    check(proc.poll() is None, f"the backend is running on {port}")

    # The startup line: source and fingerprint, no value.
    proc.stdout.readline()  # uvicorn's own boot line, timing-dependent
    log = ""
    deadline = time.time() + 10
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        log += line
        if "RAG_AUTH_TOKEN" in line and "fingerprint=" in line:
            break
    check("[auth] RAG_AUTH_TOKEN" in log, f"startup logs the auth source (log={log.strip()[-160:]!r})")
    check(FIRST not in log and SECOND not in log, "the startup line does not print the passphrase")
    check("source=backend/.env" in log, f"it names backend/.env as the source ({log.strip()[-200:]!r})")
    check("duplicate_lines_in_file=1" in log, f"it reports the duplicate line ({log.strip()[-200:]!r})")

    # Before login: refused.
    st, _ = call("/screen/config")
    check(st == 401, f"/screen/config is refused before login (got {st})")

    # The line the operator can see in the file must now be the one that works.
    st, body = call("/auth/login", {"passphrase": FIRST})
    check(st == 200, f"the visible first line logs in (got {st} {body[:120]})")
    check(any(c.name == "rag_session" for c in jar), "a session cookie was stored")

    st, body = call("/screen/config")
    check(st == 200, f"/screen/config works with the session (got {st} {body[:120]})")
    parsed = json.loads(body)
    check(parsed.get("authRequired") is True, f"/screen/config reports auth is required ({body[:200]})")

    # A fresh jar: the appended line must no longer be the accepted one.
    jar.clear()
    st, _ = call("/auth/login", {"passphrase": SECOND})
    check(
        st == 401,
        f"the shadowed appended line is NOT what the server compares against (got {st})",
    )

    st, _ = call("/auth/login", {"passphrase": "something-else"})
    check(st == 401, f"an unrelated passphrase is refused (got {st})")

    st, _ = call("/health")
    check(st == 200, f"/health is still public (got {st})")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS: a real uvicorn process loads the visible passphrase and the login flow works")
