"""Byte-for-byte check of the WebSocket<->TCP relay, with no computer and no x11vnc.

A canned RFB server is served on a real TCP port; the bridge is pointed at it
and the client side is a real WebSocket.  Any byte that fails to round-trip is a
bridge bug rather than a problem with the remote machine.

The relay under test is the fallback route, /ws/screen.  Normally the Computer
view goes to /websockify on the tunnel's own origin, which the app proxies to
websockify; this route is what covers local development and a tunnel that is not
up yet.  Both now require a session, so the test logs in first -- and checks
that the bridge refuses to carry a byte without one.
"""
import asyncio
import os
import socket
import struct
import tempfile
import threading

from fastapi.testclient import TestClient

# A session is required before the relay will speak, so the passphrase has to be
# in the environment before the app is imported.
PASS = "bridge-test-passphrase"
os.environ["RAG_AUTH_TOKEN"] = PASS
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="rag-bridge-test-"))

import app.auth as auth  # noqa: E402
import app.main as main_module  # noqa: E402
from app.main import app  # noqa: E402

# --- canned RFB server -------------------------------------------------------
GREETING = b"RFB 003.008\n"
SECLIST = bytes([1, 1])          # 1 type, type 1 = None
SECOK = struct.pack(">I", 0)
SERVER_INIT = struct.pack(">HH", 640, 480) + b"\x20" * 16 + struct.pack(">I", 4) + b"test"
PIXELS = bytes(range(256)) * 4
REPLY = PIXELS

received: list[bytes] = []


def canned_server(sock: socket.socket, ready: threading.Event) -> None:
    """Speak a canned RFB exchange, recording every byte the bridge delivers."""
    conn, _ = sock.accept()
    conn.sendall(GREETING + SECLIST)
    sent_secok = False
    sent_reply = False
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            received.append(data)
            if not sent_secok and any(bytes([1]) == c[-1:] and b"RFB" in b"".join(received[:1]) for c in received):
                conn.sendall(SECOK)
                sent_secok = True
            # The client asks for the framebuffer only after the handshake, so
            # wait until that request shows up rather than racing a timer.
            if not sent_reply and any(struct.pack(">BBHHHH", 3, 0, 0, 0, 640, 480) in c for c in received):
                conn.sendall(REPLY)
                sent_reply = True
    except OSError:
        pass
    finally:
        conn.close()


listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(1)
PORT = listener.getsockname()[1]
ready = threading.Event()
threading.Thread(target=canned_server, args=(listener, ready), daemon=True).start()

# Point the relay at the canned server instead of x11vnc's real port.  This is
# the same seam the production code uses to describe where the framebuffer is,
# so the test exercises the real resolution path -- only the answer changes.
main_module._screen_endpoint = lambda: ("127.0.0.1", PORT)  # type: ignore[assignment]

client = TestClient(app)
failures: list[str] = []

# --- the relay must refuse to carry bytes without a session ------------------
# This is the fallback path, so it is easy to forget it is gated.  If this ever
# starts passing a session through, the screen behind the tunnel is one missing
# check away from being public.
anon = TestClient(app)
refused = False
try:
    with anon.websocket_connect("/ws/screen") as ws:
        ws.receive_bytes()
except Exception:
    refused = True
if not refused:
    failures.append("/ws/screen served a frame to an unauthenticated client")

# --- and must still work with one -------------------------------------------
# TestClient keeps cookies between requests, so logging in here is what lets the
# WebSocket upgrade below carry the session.
login = client.post("/auth/login", json={"passphrase": PASS})
if login.status_code != 200:
    failures.append(f"login failed with {login.status_code}, so the relay could not be tested")

with client.websocket_connect("/ws/screen") as ws:
    got = ws.receive_bytes()
    print(f"server->client first chunk: {got!r}")
    if not got.startswith(GREETING):
        failures.append(f"expected RFB greeting, got {got!r}")

    ws.send_bytes(b"RFB 003.008\n")
    ws.send_bytes(bytes([1]))
    ws.send_bytes(bytes([1]))
    ws.send_bytes(struct.pack(">BBHi", 2, 0, 1, 0))
    ws.send_bytes(struct.pack(">BBHHHH", 3, 0, 0, 0, 640, 480))

    # The greeting, security list and SecurityResult all arrive ahead of the
    # pixels, so skip the handshake before looking for the payload.
    client_got = got[len(GREETING) + len(SECLIST) :]
    try:
        while len(client_got) < len(SECOK) + len(REPLY):
            chunk = ws.receive_bytes()
            if not chunk:
                break
            client_got += chunk
    except Exception as exc:  # noqa: BLE001
        failures.append(f"receiving the pixel reply failed: {type(exc).__name__}: {exc}")

payload = client_got[len(SECOK) :]
print(f"client received {len(payload)} of {len(REPLY)} reply bytes")
if payload[: len(REPLY)] != REPLY:
    failures.append("pixel reply did not round-trip intact")

# The canned server reads in whatever chunks arrive, so assert on the
# concatenation of everything the bridge delivered rather than on recv() sizes.
client_to_server = b"".join(received)
print(f"client->server total: {client_to_server!r}")
for expected, label in (
    (b"RFB 003.008\n", "version"),
    (bytes([1]), "security choice"),
    (struct.pack(">BBHHHH", 3, 0, 0, 0, 640, 480), "FramebufferUpdateRequest"),
):
    if expected not in client_to_server:
        failures.append(f"{label} never reached the TCP server (ws->tcp direction broken)")

if failures:
    print("\nFAIL:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("\nPASS: relay round-trips RFB bytes in both directions")
