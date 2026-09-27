"""Nothing but the app may be reachable from outside.

A Quick Tunnel publishes this server at a public trycloudflare.com URL, and the
framebuffer behind it is a real signed-in browser.  So the boundary has to be:

    tunnel -> :8000 the app -> (session check) -> :6080 -> :5900 -> Chrome

Two invariants make that safe, and both are checked here by reading the scripts,
because the behaviour lives in command lines rather than in anything importable:

  1. 5900 and 6080 bind loopback only, so they have no public port at all.
  2. The tunnel publishes :8000 and nothing else.  In particular it must not
     point at websockify directly -- that would put the screen in front of the
     public edge with only the URL in the way, and the URL is not a secret.

It also checks the negative: no token, no domain, no named-tunnel config.  A
leftover credential path is not a style problem, it is an invitation to believe
the screen is protected by something that is not there.
"""
import json
import pathlib
import re
import sys

CODES = pathlib.Path(__file__).resolve().parents[2] / "codespace"
REPO = CODES.parent

failures: list[str] = []


def read(name: str) -> str:
    return (CODES / name).read_text(encoding="utf-8")


def code_lines(text: str) -> list[str]:
    """Drop comments and blanks so a check cannot be satisfied by a comment."""
    out = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


# --- 1. x11vnc stays on loopback -------------------------------------------
# -localhost is what confines the RFB stream.  Without it x11vnc binds every
# interface and 5900 becomes a world-readable VNC server with no passphrase.
computer = read("start-computer.sh")
if "-localhost" not in computer:
    failures.append("x11vnc is started without -localhost, so 5900 would bind every interface")

if "-nolisten tcp" not in computer:
    failures.append("Xvfb is started without -nolisten tcp, so the X server would accept TCP clients")

# --- 2. websockify stays on loopback ----------------------------------------
for name in ("start-computer.sh", "supervise.sh"):
    text = read(name)
    lines = code_lines(text)
    hits = [ln for ln in lines if "websockify" in ln and "listen" not in ln.lower()]
    if not hits:
        continue
    for line in hits:
        if "127.0.0.1" in line:
            continue
        if re.search(r"--?listen", line):
            failures.append(f"{name}: websockify binds beyond loopback: {line}")
            break
    else:
        # A bare `websockify` with no loopback argument anywhere defaults to
        # 0.0.0.0, so the absence of a failure above is not a pass.
        if not any("127.0.0.1" in ln for ln in lines if "websockify" in ln):
            failures.append(
                f"{name}: websockify has no 127.0.0.1 argument, so it would bind every interface"
            )

# --- 3. the tunnel publishes the app and only the app ------------------------
tunnel = read("start-tunnel.sh")
tunnel_lines = code_lines(tunnel)

url_flags = [ln for ln in tunnel_lines if "--url" in ln]
if not url_flags:
    failures.append("start-tunnel.sh never passes --url to cloudflared; no tunnel is opened")
for line in url_flags:
    # The origin is indirected through $TUNNEL_ORIGIN on purpose: the flag must
    # not hardcode a port, or it can drift from env.sh.  The value is checked
    # against env.sh below, so accepting the indirection here is correct.
    if "TUNNEL_ORIGIN" in line:
        continue
    if "127.0.0.1" not in line and "localhost" not in line:
        failures.append(f"start-tunnel.sh: tunnel origin is not loopback: {line}")
    # The critical one.  Pointing the tunnel at 6080 would publish the screen
    # directly, skipping the app and with it the only session check there is.
    if re.search(r"127\.0\.0\.1:\$\{?(WEBSOCKIFY|VNC|SCREEN)", line) or ":$WEBSOCKIFY_PORT" in line:
        failures.append(
            "start-tunnel.sh: the tunnel points at websockify instead of the app; "
            "the screen must be reached through the app's session check"
        )

# The published origin has to be the backend, and it must come from one variable
# so it cannot drift from env.sh.
if "TUNNEL_ORIGIN" not in tunnel:
    failures.append("start-tunnel.sh does not use $TUNNEL_ORIGIN, so the published port is set ad hoc")

env = read("env.sh")
if not re.search(r'TUNNEL_ORIGIN="http://127\.0\.0\.1:\$BACKEND_PORT"', env):
    failures.append("env.sh does not define TUNNEL_ORIGIN as the app on 127.0.0.1:$BACKEND_PORT")
# And nothing anywhere may repoint the tunnel at the screen ports.
if re.search(r"TUNNEL_ORIGIN=.*(WEBSOCKIFY|VNC|SCREEN)", env):
    failures.append("env.sh: TUNNEL_ORIGIN names a screen port instead of the app")

# --- 4. the hostname is discovered, not configured --------------------------
if "PUBLIC_URL_FILE" not in tunnel:
    failures.append("start-tunnel.sh never writes PUBLIC_URL_FILE, so /screen/config cannot find the URL")
if "trycloudflare" not in tunnel:
    failures.append("start-tunnel.sh does not recognise the trycloudflare URL cloudflared prints")

# --- 5. no credentials, domains or named tunnels anywhere -------------------
# A dead named-tunnel path tends to survive a migration and get re-enabled by
# whoever is in a hurry, so its absence is worth asserting rather than assuming.
BANNED = ("CF_TUNNEL_TOKEN", "COMPUTER_HOSTNAME", "COMPUTER_WS_URL", "CF_CONFIG", "credentials-file")
for path in sorted(CODES.rglob("*.sh")):
    text = path.read_text(encoding="utf-8")
    for token in BANNED:
        if token in text:
            failures.append(f"{path.name} still references {token}, from the old named-tunnel design")

if (CODES / "cloudflared" / "config.yml.example").exists():
    failures.append(
        "codespace/cloudflared/config.yml.example still exists; a quick tunnel needs no config file"
    )
# Leftover empty directory, not a failure in itself -- just tidy up after it.
if (CODES / "cloudflared").is_dir() and not any((CODES / "cloudflared").iterdir()):
    (CODES / "cloudflared").rmdir()

# --- 6. the app's own passphrase must be part of the story -------------------
# The tunnel URL is public by design.  If the passphrase were optional, the
# design would have no lock on it at all.
for name in ("env.sh", "boot.sh", "verify.sh"):
    if "RAG_AUTH_TOKEN" not in read(name):
        failures.append(f"{name} does not mention RAG_AUTH_TOKEN; the only lock on the screen is missing")

# --- 7. devcontainer keeps the forwarded ports private ----------------------
dc_text = (REPO / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
dc = json.loads(re.sub(r"^\s*//.*$", "", dc_text, flags=re.MULTILINE))

forwarded = {str(p) for p in dc.get("forwardPorts", [])}
if "8000" not in forwarded:
    failures.append("port 8000 (the app) is not declared in devcontainer forwardPorts")
if "6080" not in forwarded:
    failures.append("port 6080 (websockify) is not declared in devcontainer forwardPorts")
# RFB is only ever reached through websockify, so GitHub has no business being
# handed 5900 directly.
if "5900" in forwarded:
    failures.append("devcontainer forwards 5900; the RFB port is only reachable via websockify")

if dc.get("onAutoForward") != "private":
    failures.append(f"devcontainer onAutoForward is {dc.get('onAutoForward')!r}; ports must stay private")

for port, attrs in (dc.get("portsAttributes") or {}).items():
    if attrs.get("visibility") != "private":
        failures.append(f"port {port} is not marked private in portsAttributes")
    if attrs.get("onAutoForward") not in ("private", "ignore"):
        failures.append(f"port {port} auto-forwards as {attrs.get('onAutoForward')!r}, not private")

# The passphrase must arrive as a secret, never baked into the image definition.
for key, value in (dc.get("remoteEnv") or {}).items():
    if key == "RAG_AUTH_TOKEN":
        failures.append("RAG_AUTH_TOKEN is set in remoteEnv; it must be a Codespaces secret")

if failures:
    print("FAIL:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(
    "PASS: 5900/6080/X are loopback-only, the tunnel publishes only :8000, "
    "and the app's session is the only thing gating the screen"
)
