"""Check that the screen URL is derived correctly, in every configuration."""
import os
import subprocess
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CASES = [
    # (env, expected, why)
    ({}, "", "no tunnel configured -> the viewer uses the backend relay"),
    ({"COMPUTER_HOSTNAME": "computer.example.com"},
     "wss://computer.example.com/websockify", "the normal production case"),
    ({"COMPUTER_HOSTNAME": "computer.example.com/"},
     "wss://computer.example.com/websockify", "a trailing slash must not leak into the path"),
    ({"COMPUTER_HOSTNAME": "https://computer.example.com"},
     "wss://computer.example.com/websockify", "a scheme is upgraded, never embedded"),
    ({"COMPUTER_HOSTNAME": "computer.example.com",
      "COMPUTER_WS_URL": "wss://screen.example.com/rfb"},
     "wss://screen.example.com/rfb", "the explicit URL always wins"),
]

probe = "import app.config as c; print(c.load_settings().computer_ws_url)"

failed = 0
for env, expected, why in CASES:
    for key in ("COMPUTER_HOSTNAME", "COMPUTER_WS_URL"):
        os.environ.pop(key, None)
    os.environ.update(env)
    run = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=BACKEND_DIR,
    )
    got = run.stdout.strip()
    ok = run.returncode == 0 and got == expected
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {env or '(no env)'} -> {got!r}")
    if not ok:
        print(f"     expected {expected!r} -- {why}")
        if run.stderr.strip():
            print(f"     stderr: {run.stderr.strip().splitlines()[-1]}")

print()
print("all screen URL cases pass" if not failed else f"{failed} case(s) failed")
sys.exit(1 if failed else 0)
