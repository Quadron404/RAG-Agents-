"""The screen socket is routed through the authenticated Worker, never the tunnel.

The Computer view opens a WebSocket to ``/websockify``.  That socket is
authenticated the same way the rest of the app is: the browser proves who it is
once, and the Worker injects the internal token the backend screen endpoint
requires.  The raw Quick Tunnel bypasses the Worker, so a browser pointed at it
has its upgrade refused with a 403 -- which is exactly the failure this route
used to cause by advertising the tunnel origin.

So ``/screen/config`` reports the authenticated gateway origin, which is the same
origin the app is served from (``WORKER_ORIGIN``, or ``PUBLIC_BASE_URL`` when the
Worker is not named).  The Quick Tunnel URL is still read and validated, but only
to drive the UI's "the tunnel moved / went stale" note; it is deliberately never
the address the socket connects to.

These cases pin both halves: the socket always goes through a gateway we can
vouch for, and a missing or garbage tunnel file never turns into an advertised
URL.  With no gateway at all the viewer uses the same-origin bridge, which is
authenticated the same way.
"""
import os
import subprocess
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The Worker the browser actually reaches, and the socket origin derived from it.
GATEWAY = "https://rag-agents.example.workers.dev"
GATEWAY_WS = "wss://rag-agents.example.workers.dev/websockify"
TUNNEL = "https://fresh-olive-cheese-1234.trycloudflare.com"


def read_config(path: str, extra_env: dict) -> tuple[dict, str]:
    """Ask the running app to describe the screen route, in a fresh process.

    ``WORKER_ORIGIN`` and ``PUBLIC_BASE_URL`` are cleared from the inherited
    environment and set only by the case under test, so a developer's shell
    cannot decide the answer.  The route requires an authenticated session (the
    whole point of the post-auth app), so the probe signs in through the app's
    own session machinery before asking.
    """
    probe = (
        "import os, tempfile\n"
        "os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='rag-screen-probe-')\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app, db, auth\n"
        "import json\n"
        "_u = db.upsert_user('screen-probe', 'probe@example.com', True, 'Probe', '')\n"
        "_rec = auth.sessions.create(_u['id'])\n"
        "c = TestClient(app)\n"
        "c.cookies.set('rag_session', _rec['token'])\n"
        "print(json.dumps(c.get('/screen/config').json()))\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("WORKER_ORIGIN", "PUBLIC_BASE_URL")
    }
    env["PUBLIC_URL_FILE"] = path
    env.update(extra_env)
    run = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        env=env,
    )
    if run.returncode != 0:
        return {}, (run.stderr.strip().splitlines() or ["no output"])[-1]
    import json

    return json.loads(run.stdout.strip()), ""


# (tunnel file contents or None, gateway env, expected mode, expected wsUrl,
#  expected publicOrigin, why)
CASES = [
    # A gateway is configured: the socket always goes through it, whatever the
    # tunnel file says -- the tunnel is only reported, never dialled.
    (None, {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, "",
     "no tunnel file yet; the Worker is still the route"),
    ("", {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, "",
     "an empty tunnel file is not a URL, and does not change the socket route"),
    (TUNNEL, {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, TUNNEL,
     "the live tunnel is reported for the UI note, not used as the socket"),
    (TUNNEL + "\n", {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, TUNNEL,
     "a trailing newline from the tunnel script is stripped"),
    ("http://127.0.0.1:8000", {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, "",
     "a loopback origin is the local machine, not a tunnel, so it is never reported"),
    ("https://evil.example.com", {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, "",
     "an arbitrary hostname is ignored; only trycloudflare origins are reported"),
    ("https://x.trycloudflare.com.evil.com", {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, "",
     "a lookalike that merely contains the string is not a tunnel URL"),
    ("not a url at all", {"WORKER_ORIGIN": GATEWAY}, "tunnel", GATEWAY_WS, "",
     "garbage must not reach the viewer as a URL"),
    # No gateway: the same-origin bridge, which is authenticated the same way --
    # and still never the raw tunnel.
    (TUNNEL, {}, "bridge", "", TUNNEL,
     "without a Worker the viewer uses the in-app bridge, never the raw tunnel"),
    (None, {}, "bridge", "", "",
     "no gateway and no tunnel -> the bridge, with nothing to report"),
    # PUBLIC_BASE_URL is the same origin when the Worker is not named.
    (None, {"PUBLIC_BASE_URL": "https://app.example.dev"}, "tunnel",
     "wss://app.example.dev/websockify", "",
     "public_base_url is the origin when worker_origin is unset"),
    (None, {"WORKER_ORIGIN": GATEWAY, "PUBLIC_BASE_URL": "https://app.example.dev"}, "tunnel",
     GATEWAY_WS, "",
     "worker_origin is the canonical gateway when both are set"),
]

failed = 0
for contents, gateway, want_mode, want_ws, want_public, why in CASES:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        if contents is not None:
            fh.write(contents)
        path = fh.name
    try:
        cfg, err = read_config(path, gateway)
        if err:
            print(f"FAIL {contents!r} gateway={gateway} -> probe failed: {err}")
            failed += 1
            continue
        mode, ws, public = cfg.get("mode"), cfg.get("wsUrl"), cfg.get("publicOrigin")
        ok = mode == want_mode and ws == want_ws and public == want_public
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} tunnel={contents!r} -> mode={mode} wsUrl={ws!r} publicOrigin={public!r}")
        if not ok:
            print(f"     expected mode={want_mode} wsUrl={want_ws!r} publicOrigin={want_public!r} -- {why}")
    finally:
        os.unlink(path)

# The invariant the whole fix rests on: the advertised socket is never the raw
# tunnel, even in the case where the tunnel is live and valid.
print()
print("all screen route cases pass" if not failed else f"{failed} case(s) failed")
sys.exit(1 if failed else 0)
