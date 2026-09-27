"""The live screen must never be reachable except through Cloudflare Access.

This is the invariant that makes the whole design safe: the framebuffer of a
real, signed-in browser is exposed, so if either of these two ports ever binds to
a non-loopback address, the screen is on the open internet with no
authentication in front of it.

The scripts are checked by reading them, because the binding behaviour lives in
the command lines, not in anything importable.
"""
import pathlib
import re
import sys

CODES = pathlib.Path(__file__).resolve().parents[2] / "codespace"

failures: list[str] = []

# --- x11vnc must not listen beyond loopback ---------------------------------
# x11vnc's -localhost is what confines the RFB stream. Without it, x11vnc binds
# every interface and 5900 becomes a world-readable VNC server.
x11vnc = (CODES / "start-computer.sh").read_text(encoding="utf-8")
if "-localhost" not in x11vnc:
    failures.append("x11vnc is started without -localhost, so 5900 would bind every interface")

# The old QEMU-era script used -rfbport with no loopback restriction; make sure
# nothing reintroduces that shape.
if re.search(r"-rfbport\s+\$?\{", x11vnc) and "-localhost" not in x11vnc:
    failures.append("x11vnc binds an explicit port without -localhost")

# --- websockify must not listen beyond loopback -----------------------------
# 6080 carries the RFB bytes as a WebSocket. It is a published screen with
# nobody in front of it unless Cloudflare Access is doing its job.
for name in ("start-computer.sh", "supervise.sh"):
    text = (CODES / name).read_text(encoding="utf-8")
    if "websockify" not in text:
        continue
    lines = [ln for ln in text.splitlines() if "websockify" in ln and not ln.strip().startswith("#")]
    for line in lines:
        if "127.0.0.1" in line:
            continue
        # A listen address may also be spelled as an explicit bind flag.
        if re.search(r"--?listen", line) and "127.0.0.1" not in line:
            failures.append(f"{name}: websockify binds beyond loopback: {line.strip()}")
            break
    else:
        # Started as a bare command with no loopback argument anywhere: the
        # default is 0.0.0.0, so this is a failure, not a pass.
        if not any("127.0.0.1" in ln for ln in text.splitlines()):
            failures.append(
                f"{name}: websockify is started with no 127.0.0.1 argument; "
                "it would default to binding every interface"
            )

# --- Xvfb must not open a TCP port -------------------------------------------
# -nolisten tcp is what stops the X server itself being a remote-control
# surface. -ac disables access control, which is only acceptable because the
# socket is a local filesystem entry.
if "-nolisten tcp" not in x11vnc:
    failures.append("Xvfb is started without -nolisten tcp, so the X server would accept TCP clients")

# --- devcontainer must not publish the ports --------------------------------
devcontainer = (CODES.parent / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
# Parse it rather than grepping: the check is about what is *declared*, and a
# substring match would happily pass on a port number mentioned in a comment.
import json  # noqa: E402

stripped = re.sub(r"^\s*//.*$", "", devcontainer, flags=re.MULTILINE)
dc = json.loads(stripped)

forwarded = {str(p) for p in dc.get("forwardPorts", [])}
if "6080" not in forwarded:
    failures.append("port 6080 (websockify) is not declared in devcontainer forwardPorts")
if "8000" not in forwarded:
    failures.append("port 8000 (the app) is not declared in devcontainer forwardPorts")
# RFB itself is only ever reached through websockify, so GitHub has no business
# being handed 5900.
if "5900" in forwarded:
    failures.append("devcontainer forwards 5900; the RFB port is only reachable via websockify")

if dc.get("onAutoForward") not in ("private", "notify", "openBrowser"):
    failures.append(
        f"devcontainer onAutoForward is {dc.get('onAutoForward')!r}; ports must not be world-readable"
    )
if dc.get("onAutoForward") in ("notify", "openBrowser"):
    failures.append("devcontainer auto-forwarding is not restricted to private visibility")

for port, attrs in (dc.get("portsAttributes") or {}).items():
    if attrs.get("visibility") != "private":
        failures.append(f"port {port} is not marked private in portsAttributes")
    if attrs.get("onAutoForward") not in ("private", "ignore"):
        failures.append(f"port {port} auto-forwards as {attrs.get('onAutoForward')!r}, not private")

# The token must arrive as a secret, never baked into the image definition.
if "CF_TUNNEL_TOKEN" in dc.get("remoteEnv", {}):
    failures.append("CF_TUNNEL_TOKEN is set in remoteEnv; it must be a Codespaces secret")

# --- the token must never be committed ---------------------------------------
example = (CODES / "cloudflared" / "config.yml.example").read_text(encoding="utf-8")
if re.search(r"eyJh", example) or "token:" in example.lower():
    failures.append("the example tunnel config appears to contain a real token")
if "trycloudflare" in example and "Do NOT" not in example:
    failures.append("the example config references a quick tunnel, which cannot sit behind Access")

if failures:
    print("FAIL:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("PASS: VNC, websockify and X are loopback-only and the ports are private")
