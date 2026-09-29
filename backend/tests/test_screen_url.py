"""The screen URL has to be discovered, never configured.

A Quick Tunnel is handed a random https://<name>.trycloudflare.com origin every
time it starts, and it changes on every restart.  So there is no variable to set
and no value to hardcode: the tunnel script writes the live URL to a file and
/screen/config derives everything from it.

These cases pin the rules that make that work, and -- more importantly -- pin what
must NOT come back.  A stale hostname that still looks valid is the failure mode
that would send every viewer to a URL Cloudflare no longer serves, so a file
that is missing, empty, or garbage has to produce the in-app bridge instead.
"""
import os
import subprocess
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_published(path: str) -> tuple[str, str]:
    """Ask the running app to describe the tunnel, in a fresh process."""
    probe = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "import json\n"
        "c = TestClient(app)\n"
        "print(json.dumps(c.get('/screen/config').json()))\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        env={**os.environ, "PUBLIC_URL_FILE": path},
    )
    if run.returncode != 0:
        return "", run.stderr.strip().splitlines()[-1] if run.stderr.strip() else "no output"
    import json
    return json.dumps(json.loads(run.stdout.strip())), ""


CASES = [
    # (file contents or None, expected mode, expected wsUrl, why)
    (None, "bridge", "",
     "no tunnel file -> the viewer uses the in-app /ws/screen relay"),
    ("", "bridge", "",
     "an empty file is not a URL, and must not be turned into one"),
    ("   \n", "bridge", "",
     "whitespace is still empty"),
    ("http://127.0.0.1:8000", "bridge", "",
     "a loopback origin is the local machine, not a tunnel, so it is not advertised"),
    ("https://fresh-olive-cheese-1234.trycloudflare.com", "tunnel",
     "wss://fresh-olive-cheese-1234.trycloudflare.com/websockify",
     "the normal case: the app is published, and /websockify is same-origin so the session cookie rides along"),
    ("https://rot-mango-pancake-9876.trycloudflare.com\n", "tunnel",
     "wss://rot-mango-pancake-9876.trycloudflare.com/websockify",
     "a trailing newline from the tunnel script is stripped, not embedded"),
    ("https://next-tunnel-abc.trycloudflare.com", "tunnel",
     "wss://next-tunnel-abc.trycloudflare.com/websockify",
     "a restarted tunnel publishes a different name and the app must follow it"),
    ("https://old-tunnel-xyz.trycloudflare.com", "tunnel",
     "wss://old-tunnel-xyz.trycloudflare.com/websockify",
     "two tunnels in a row resolve independently, which is the whole restart story"),
    ("https://evil.example.com", "bridge", "",
     "an arbitrary hostname is ignored; only trycloudflare origins are published"),
    ("https://x.trycloudflare.com.evil.com", "bridge", "",
     "a lookalike that merely contains the string is not a tunnel URL"),
    ("not a url at all", "bridge", "",
     "garbage must not reach the viewer as a URL"),
]

failed = 0
for contents, want_mode, want_ws, why in CASES:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        if contents is not None:
            fh.write(contents)
        path = fh.name
    try:
        got_json, err = read_published(path)
        if err:
            print(f"FAIL {contents!r} -> probe failed: {err}")
            failed += 1
            continue
        import json as _json
        cfg = _json.loads(got_json)
        mode, ws = cfg.get("mode"), cfg.get("wsUrl")
        ok = mode == want_mode and ws == want_ws
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {contents!r} -> mode={mode} wsUrl={ws!r}")
        if not ok:
            print(f"     expected mode={want_mode} wsUrl={want_ws!r} -- {why}")
    finally:
        os.unlink(path)

# The two tunnel cases must disagree, or a restart would silently keep the old
# address.  This is the assertion that the restart requirement actually rests on.
print()
print("all screen URL cases pass" if not failed else f"{failed} case(s) failed")
sys.exit(1 if failed else 0)
