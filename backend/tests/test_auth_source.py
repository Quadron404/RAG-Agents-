"""The passphrase must come from one place, and the server must say which one.

These tests exist because "incorrect passphrase" has three unrelated causes and
one error message: the value was never loaded, the value was loaded from a
different place than the operator is reading, or the operator typed it wrong.  A
login test that only checks the happy path passes in all three, so each cause
gets its own case, and the resolution rules are checked against the exact file
shapes that used to disagree.

The login flow is exercised for real, over HTTP, against the actual app, with
cookies -- not by calling the comparison function directly.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
BASH = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    if ok:
        print(f"ok    {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}")
    return ok


def sandbox(env_text: str, env: dict | None = None) -> tuple[Path, dict]:
    """A throwaway copy of backend/ with a real .env, so config.py is tested verbatim.

    The loader resolves .env relative to config.py's own __file__, not the working
    directory, so a temp .env beside a temp cwd is never read.  Copying the
    package is the only way to point the real, unmodified loader at a different
    file -- and testing the real file is the point, because the disagreement this
    is about was between two copies of this logic.
    """
    d = Path(tempfile.mkdtemp())
    backend = d / "backend"
    backend.mkdir()
    shutil.copytree(APP, backend / "app")
    (backend / ".env").write_text(env_text, encoding="utf-8")
    return backend, {**os.environ, **(env or {})}


def describe_in(backend: Path, env: dict) -> dict:
    """Run `describe("RAG_AUTH_TOKEN")` in a fresh interpreter against `backend`."""
    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.insert(0, {str(backend)!r})
        from app import config
        print(json.dumps(config.describe("RAG_AUTH_TOKEN")))
        """
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"_error": (r.stdout + r.stderr)[-400:]}


def with_env_file(contents: str, env: dict | None = None):
    backend, run_env = sandbox(contents, env)
    return describe_in(backend, run_env)


def fingerprint(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:12]


# --- 1. one rule for which line of the file wins ----------------------------
# The bug: boot.sh did `set -a; . ./.env`, so bash took the LAST
# RAG_AUTH_TOKEN line and config.py's loader was skipped entirely for it
# (`if key not in os.environ`).  The operator read the first line; the server
# compared against the last.  Duplicate lines are the expected state of a
# reconfigured Codespace, since the documented way to set this is an append.
for label, env_text, expect in [
    ("first definition wins when the file was appended to twice",
     "RAG_AUTH_TOKEN=first-good\nRAG_AUTH_TOKEN=second\n", "first-good"),
    ("an empty line appended after a good one does not blank the token",
     "RAG_AUTH_TOKEN=first-good\nRAG_AUTH_TOKEN=\n", "first-good"),
    ("a commented-out placeholder is not a definition",
     "# RAG_AUTH_TOKEN=change-me\nRAG_AUTH_TOKEN=real-one\n", "real-one"),
    ("a quoted value loses its quotes",
     'RAG_AUTH_TOKEN="quoted value"\n', "quoted value"),
    ("an unquoted value containing spaces is taken whole, not truncated at the space",
     "RAG_AUTH_TOKEN=two words\n", "two words"),
    ("a value containing $ is not expanded",
     "RAG_AUTH_TOKEN=$NOT_A_VAR\n", "$NOT_A_VAR"),
    ("a value containing # is not truncated at a comment",
     "RAG_AUTH_TOKEN=abc#def\n", "abc#def"),
]:
    info = with_env_file(env_text)
    got = info.get("fingerprint", "")
    want = fingerprint(expect)
    check(got == want, f"{label} (got {got or 'nothing'}, want {want})")

info = with_env_file("RAG_AUTH_TOKEN=first-good\nRAG_AUTH_TOKEN=second\n")
check(
    info.get("duplicate_lines_in_file") == 1,
    f"a duplicate RAG_AUTH_TOKEN line is reported, not silently ignored (got {info.get('duplicate_lines_in_file')!r})",
)

# --- 2. a real environment variable still wins, and says so -----------------
# This is the Codespaces secret path, and it must keep working.
info = with_env_file("RAG_AUTH_TOKEN=from-the-file\n", env={"RAG_AUTH_TOKEN": "from-the-secret"})
check(
    info.get("source") == "environment",
    f"a Codespaces secret wins over backend/.env (source={info.get('source')!r})",
)
check(
    info.get("shadowed_file_value") is True,
    "when the environment shadows the file, the diagnostic says so",
)
check(
    info.get("fingerprint") == fingerprint("from-the-secret"),
    "the fingerprint is of the value actually in use, not the one in the file",
)

# --- 3. the diagnostic never contains the value -----------------------------
secret = "SUPER-SECRET-do-not-print-me-1234"
info = with_env_file(f"RAG_AUTH_TOKEN={secret}\n")
blob = json.dumps(info)
check(secret not in blob, "describe() does not include the passphrase")
check(info.get("loaded") is True, "describe() reports that a token is loaded")
check(len(info.get("fingerprint", "")) == 12, "the fingerprint is a short hash")

info = with_env_file("# nothing here\n")
check(info.get("loaded") is False, "describe() reports loaded=false when unset")
check(info.get("source") == "unset", "describe() reports source=unset when unset")

# --- 4. the CLI shows the source and compares, without printing the value ----
secret = "SUPER-SECRET-do-not-print-me-1234"
cli_backend, cli_env = sandbox(f"RAG_AUTH_TOKEN={secret}\n")


def run_cli(*args: str) -> str:
    r = subprocess.run(
        [sys.executable, "-m", "app.authdiag", *args],
        capture_output=True,
        text=True,
        cwd=str(cli_backend),
        env={**cli_env, "PYTHONPATH": str(cli_backend)},
    )
    return r.stdout + r.stderr


out = run_cli()
check(secret not in out, "app.authdiag does not print the passphrase")
check("loaded" in out and "fingerprint" in out, "app.authdiag reports loaded and fingerprint")
check("backend/.env" in out, f"app.authdiag names the source (out={out.strip()[:160]!r})")

out = run_cli("--check", secret)
check("MATCHES" in out, f"--check accepts the right passphrase (out={out.strip()[:160]!r})")
check(secret not in out, "--check does not echo the passphrase it was given")

out = run_cli("--check", "wrong-one")
check("does NOT match" in out, f"--check rejects the wrong passphrase (out={out.strip()[:160]!r})")

# The value that used to be invisible: appended twice, so the server takes the
# first and the operator is probably reading the last.
dup_backend, dup_env = sandbox("RAG_AUTH_TOKEN=first-good\nRAG_AUTH_TOKEN=second\n")
r = subprocess.run(
    [sys.executable, "-m", "app.authdiag", "--check", "first-good"],
    capture_output=True, text=True, cwd=str(dup_backend),
    env={**dup_env, "PYTHONPATH": str(dup_backend)},
)
out = r.stdout + r.stderr
check("MATCHES" in out, f"--check matches the line the server actually uses ({out.strip()[:200]!r})")
check("duplicate" in out.lower(), f"--check says the file has duplicate lines ({out.strip()[:200]!r})")

# --- 5. boot.sh no longer parses .env as a shell script ----------------------
# The second parser is the actual bug, so its absence is a check, not a
# preference.  If this comes back, first-wins/last-wins disagree again.
boot = (BACKEND.parent / "codespace" / "boot.sh").read_text(encoding="utf-8")
check(
    not any(
        line.strip().startswith(("set -a", ". ./.env", "source ./.env"))
        for line in boot.splitlines()
    ),
    "boot.sh does not source backend/.env as a shell script",
)
check("config._load_dotenv" in boot, "boot.sh points at the single parser instead")

# --- 6. the real login flow, over HTTP, with the real app --------------------
# Everything above tests the loader in isolation.  This signs in for real and
# then uses the resulting session cookie on a protected route, because "the
# fingerprint matches" is not the same claim as "you can log in".
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "the-real-passphrase-9f3c"
os.environ["RAG_AUTH_TOKEN"] = TOKEN
importlib.reload(importlib.import_module("app.config"))
import app.main as main_mod  # noqa: E402

client = TestClient(main_mod.app)

r = client.post("/auth/login", json={"passphrase": "wrong"})
check(r.status_code == 401, f"a wrong passphrase is refused (got {r.status_code})")
check("incorrect passphrase" in r.json().get("error", ""), "the refusal says which it was")

# Protected before login.
r = client.get("/screen/config")
check(r.status_code == 401, f"/screen/config is refused before login (got {r.status_code})")

r = client.post("/auth/login", json={"passphrase": TOKEN})
check(r.status_code == 200, f"the configured passphrase logs in (got {r.status_code} {r.text[:120]})")
check("rag_session" in r.cookies, "a session cookie is issued")

r = client.get("/screen/config")
check(
    r.status_code == 200,
    f"/screen/config works with the session cookie (got {r.status_code} {r.text[:160]})",
)

# The security model is unchanged: still fails closed, still not public.
saved = os.environ.pop("RAG_AUTH_TOKEN")
importlib.reload(importlib.import_module("app.config"))
importlib.reload(main_mod)
noauth = TestClient(main_mod.app)
r = noauth.get("/screen/config")
check(
    r.status_code == 401,
    f"with no passphrase configured, /screen/config still refuses (got {r.status_code})",
)
r = noauth.post("/auth/login", json={"passphrase": "anything"})
check(
    r.status_code == 503,
    f"with no passphrase configured, login reports misconfiguration (got {r.status_code})",
)
r = noauth.get("/health")
check(r.status_code == 200, "/health is still public")
os.environ["RAG_AUTH_TOKEN"] = saved

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS: one parser, one passphrase, and the login flow really works")
