"""Tests for codespace/supervise.sh.

This supervisor crashed on its first line of real work, and the cause was a
construct that no amount of reading `bash -n` will catch: `"$CHILD_PID[agent]"`
is not an array lookup.  So the tests here are in two layers.

Static: a lint that rejects the bare form anywhere in the shell scripts, plus
the invariants that must not be traded away to make it work (`set -u` still on,
the agent still loopback-only, port 9000 never published).

Behavioural: where bash exists, the functions are sourced and really run against
a stand-in agent on a real socket.  "Really run" is the point -- the bug lived
in expansion semantics, so a test that only reads the text would have passed over
it happily.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODES = ROOT / "codespace"
SUPERVISE = CODES / "supervise.sh"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    if ok:
        print(f"ok    {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}")
    return ok


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


# --- 1. the bare array form is gone -----------------------------------------
#
# "$CHILD_PID[agent]" expands $CHILD_PID -- element 0, never assigned -- and
# then appends the literal text "[agent]".  Under `set -u` that is "unbound
# variable".  The braced form ${CHILD_PID[agent]} is the only correct one, so
# the bare form is a lint failure everywhere in codespace/*.sh, not just in the
# three places that happened to be reached.
BARE_ARRAY = re.compile(r'"\$\{?[A-Za-z_][A-Za-z0-9_]*\[[^"\]]+\]\}?"')

for script in sorted(CODES.glob("*.sh")):
    text = strip_comments(script.read_text(encoding="utf-8"))
    for lineno, line in enumerate(text.splitlines(), 1):
        m = BARE_ARRAY.search(line)
        if not m:
            continue
        token = m.group(0)
        # A braced reference is correct; the bare form is the bug.
        if token.startswith('"${'):
            continue
        failures.append(
            f"{script.name}:{lineno} uses {token} - bash reads that as "
            "$NAME (element 0) plus literal text, not as an array element; "
            'use "${NAME[key]}"'
        )
        print(f"FAIL  {script.name}:{lineno} bare array expansion: {token}")

check(
    not [f for f in failures if "bare array expansion" in f or "uses \"$" in f],
    "no shell script uses a bare \"$NAME[key]\" expansion",
)

supervise_text = SUPERVISE.read_text(encoding="utf-8")

# --- 2. set -u is not the thing that was wrong ------------------------------
#
# The tempting fix is to drop `set -u`, which makes the crash disappear and takes
# every other unset-variable bug in the script with it.  Assert it is still on.
check(
    re.search(r"^set -[a-z]*u", supervise_text, re.M) is not None,
    "supervise.sh still runs under `set -u` (the bug was not disabled)",
)

# --- 3. agent startup is idempotent -----------------------------------------
agent_fn = re.search(r"^start_agent\(\) \{.*?^\}", supervise_text, re.M | re.S)
if check(agent_fn is not None, "supervise.sh defines start_agent"):
    body = agent_fn.group(0)
    # The guard has to come before the launch, or it is decoration.
    guard_at = body.find("agent_present")
    launch_at = body.find("setsid python3")
    check(
        guard_at != -1 and launch_at != -1 and guard_at < launch_at,
        "start_agent checks for a running agent *before* launching one",
    )
    check(
        "Address already in use" in body,
        "start_agent explains the occupied-port case instead of retrying the bind",
    )
    check(
        re.search(r"if\s+agent_present", body) is not None,
        "start_agent adopts a running agent rather than spawning a duplicate",
    )

# The health loop must consult the port, not only the pid, or an adopted agent is
# respawned every tick.
loop = supervise_text[supervise_text.find("while true; do"):]
check(
    re.search(r"child_alive agent\s*&&\s*!\s*agent_present", loop) is not None,
    "the health loop treats an adopted agent as healthy",
)

# --- 4. loopback only, and 9000 is never published ---------------------------
# start_agent must not pass --host, and must not set AGENT_HOST, so the daemon's
# own 127.0.0.1 default stands.
check(
    "--host" not in agent_fn.group(0) if agent_fn else False,
    "start_agent does not pass --host to the agent",
)
check(
    re.search(r"^\s*AGENT_HOST=", agent_fn.group(0), re.M) is None if agent_fn else False,
    "start_agent does not override AGENT_HOST (daemon stays on its 127.0.0.1 default)",
)
check(
    'TUNNEL_ORIGIN="http://127.0.0.1:$BACKEND_PORT"' in (CODES / "env.sh").read_text(encoding="utf-8"),
    "the tunnel still publishes only the app on 127.0.0.1, not the agent port",
)
check(
    f"$AGENT_PORT" not in (CODES / "env.sh").read_text(encoding="utf-8").split("TUNNEL_ORIGIN")[-1],
    "the agent port is not part of the published origin",
)

# --- 5. behaviour, for real, where bash exists ------------------------------
BASH = shutil.which("bash")

agent_status = {"ok": True, "workspace": "/workspaces/rag-browser-lab", "pid": 4242}


class _Handler(BaseHTTPRequestHandler):
    payload = agent_status

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep the test output readable
        pass


class _NotAnAgent(BaseHTTPRequestHandler):
    """Answers on the port, but is not the agent."""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"hi")

    def log_message(self, *a):
        pass


def serve(handler_cls) -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


if BASH:
    # Nothing in these probes may really start anything.  Stub the launchers and
    # point AGENT_PY at a throwaway file so start_websockify/start_agent reach
    # the line that crashed -- `echo "${CHILD_PID[x]}"` -- and then do nothing.
    STUBS = f'''
setsid() {{ "$@" & }}
python3() {{ exit 0; }}
websockify() {{ exit 0; }}
'''

    def bash_probe(port: int) -> tuple[int, str]:
        """Source supervise.sh and ask it what it thinks the agent port holds."""
        script = f"""
set -uo pipefail
{STUBS}
export AGENT_PORT={port}
export DESKTOP_RUN_DIR="$(mktemp -d)"
export DESKTOP_LOG_DIR="$DESKTOP_RUN_DIR"
export AGENT_PY="$DESKTOP_RUN_DIR/fake-agent.py"
: > "$AGENT_PY"
mkdir -p "$DESKTOP_RUN_DIR"
source "{SUPERVISE.as_posix()}" >/dev/null 2>&1
if agent_present; then echo PRESENT; else echo ABSENT; fi
if port_occupied; then echo OCCUPIED; else echo FREE; fi
if start_agent >/dev/null 2>&1; then echo START_OK; else echo START_FAIL; fi
echo "agent-pidfile: $(cat "$DESKTOP_RUN_DIR/supervisor-agent.pid" 2>/dev/null)"
"""
        r = subprocess.run(
            [BASH, "-c", script], capture_output=True, text=True, timeout=90
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    # (a) a real agent on the port -> PRESENT, and start_agent is a no-op.
    srv, port = serve(_Handler)
    try:
        code, out = bash_probe(port)
        check(code == 0, f"supervise.sh sources cleanly under set -u (rc={code}) {out.strip().replace(chr(10)," | ")[:90]}")
        check("PRESENT" in out, f"agent_present detects a running agent {out.strip().replace(chr(10)," | ")[:90]}")
        check("START_OK" in out, f"start_agent is a no-op when the agent is already up {out.strip().replace(chr(10)," | ")[:90]}")
        check("unbound variable" not in out, "no 'unbound variable' with the agent already running")
    finally:
        srv.shutdown()

    # (b) nothing on the port -> ABSENT and FREE, and the launch path is reached
    # without tripping set -u.  This is the original crash, reached deliberately.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    code, out = bash_probe(free_port)
    check(code == 0, f"supervise.sh sources cleanly with a free port (rc={code}) {out.strip().replace(chr(10)," | ")[:90]}")
    check("ABSENT" in out, f"agent_present is false on a free port {out.strip().replace(chr(10)," | ")[:90]}")
    check("FREE" in out, f"port_occupied is false on a free port {out.strip().replace(chr(10)," | ")[:90]}")
    check("unbound variable" not in out, "no 'unbound variable' on the launch path")
    pid = re.search(r"agent-pidfile:\s*(\S+)", out)
    check(
        pid is not None and pid.group(1).isdigit(),
        f"the agent pid file holds a pid, not a literal '[agent]' (got {pid.group(1) if pid else 'nothing'})",
    )

    # (c) a foreign process on the port -> start_agent must refuse rather than
    # bind-fail forever.  This is the EADDRINUSE case from the bug report.
    srv, port = serve(_NotAnAgent)
    try:
        code, out = bash_probe(port)
        check("START_FAIL" in out, f"start_agent refuses to launch against a foreign process {out.strip().replace(chr(10)," | ")[:90]}")
        check("unbound variable" not in out, "no 'unbound variable' on the occupied path")
    finally:
        srv.shutdown()

    # (d) the other two crash sites.  start_websockify and start_tunnel each
    # wrote "$CHILD_PID[x]" to a pid file; neither had been reached, because the
    # supervisor died on the agent one first.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    vnc_port = listener.getsockname()[1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        ws_port = s.getsockname()[1]
    script = f"""
set -uo pipefail
{STUBS}
export AGENT_PORT=1
export VNC_PORT={vnc_port}
export WEBSOCKIFY_PORT={ws_port}
export RUN_DIR="$(mktemp -d)"
export DESKTOP_RUN_DIR="$RUN_DIR"
export DESKTOP_LOG_DIR="$RUN_DIR"
export AGENT_PY="$RUN_DIR/fake-agent.py"
: > "$AGENT_PY"
mkdir -p "$RUN_DIR"
source "{SUPERVISE.as_posix()}" >/dev/null 2>&1
start_websockify >/dev/null 2>&1
echo "ws-pidfile: $(cat "$RUN_DIR/websockify.pid" 2>/dev/null)"
"""
    try:
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=90)
        combined = (r.stdout or "") + (r.stderr or "")
        check(
            "unbound variable" not in combined,
            f"start_websockify writes its pid file without tripping set -u {combined.strip().replace(chr(10)," | ")[:90]}",
        )
        wpid = re.search(r"ws-pidfile:\s*(\S+)", combined)
        check(
            wpid is not None and wpid.group(1).isdigit(),
            f"the websockify pid file holds a pid, not a literal '[websockify]' "
            f"(got {wpid.group(1) if wpid else 'nothing'})",
        )
    finally:
        listener.close()
else:
    print("skip  behavioural checks (no bash on this host)")

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("PASS: the supervisor starts, adopts a running agent, and stays under set -u")
