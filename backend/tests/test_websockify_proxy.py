"""The /websockify proxy has to carry real bytes, not just accept connections.

/websockify is the route the Computer view actually uses in a deployed
Codespace: the browser opens it on the tunnel's origin and the app relays to
websockify on 127.0.0.1:6080.  It is the last hop before a live, signed-in
browser, so two things matter and neither is visible from a status code:

  * frames must survive the relay in both directions, as raw binary -- noVNC
    negotiates the "binary" subprotocol and a text frame would corrupt RFB;
  * the subprotocol the client asked for has to be the one echoed back, because
    noVNC refuses the connection if it is not.

A real WebSocket server stands in for websockify so the test exercises a genuine
handshake, genuine subprotocol negotiation and genuine binary framing rather than
a mock.  It runs on its own loop in a background thread, because TestClient
drives the app on a separate event loop and the stub has to be able to accept the
connection while that happens.

Run with:  python -m tests.test_websockify_proxy
"""
import asyncio
import os
import socket
import tempfile
import threading
import time

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="rag-wsproxy-test-"))

from fastapi.testclient import TestClient  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

import app.main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.vm import websockify_proxy  # noqa: E402

failures: list[str] = []


async def stub_websockify(ws) -> None:
    """Answer one frame with the same bytes plus a marker.

    Echoing the client's own bytes back is what proves the relay moved them
    intact: if the app forwarded anything as text, or re-framed them, the
    comparison fails rather than the connection merely looking healthy.
    """
    async for message in ws:
        if isinstance(message, str):
            # A text frame here would mean something upstream re-encoded RFB.
            failures.append(f"stub received a text frame, not binary: {message!r}")
            return
        await ws.send(message + b"|from-server")
        return


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_stub(port: int) -> tuple[threading.Thread, dict]:
    """Run the stub on its own loop in a thread, and wait until it accepts."""
    state: dict = {"ready": threading.Event(), "stop": False}

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def boot():
            server = await serve(stub_websockify, "127.0.0.1", port, subprotocols=["binary"])
            state["server"] = server
            state["ready"].set()
            while not state["stop"]:
                await asyncio.sleep(0.05)
            await server.close()

        try:
            loop.run_until_complete(boot())
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    state["ready"].wait(10)
    time.sleep(0.2)
    return thread, state


def check(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main() -> int:
    port = free_port()
    _thread, state = start_stub(port)

    # The endpoint builds its target from computer.websockify_port, which reads
    # settings.screen_ws_port -- exactly as production does.  Retargeting the
    # setting is enough to aim the relay at the stub, and leaves the real
    # resolution path (property -> settings) in the loop.
    original_port = main_module.computer.settings.screen_ws_port
    main_module.computer.settings.screen_ws_port = port

    try:
        # The relay itself: /websockify is no longer behind a session gate, so
        # this is now simply "does the byte path work".
        user = TestClient(app)
        echoed = None
        protocol = None
        try:
            with user.websocket_connect("/websockify", subprotocols=["binary"]) as ws:
                protocol = ws.accepted_subprotocol
                check(protocol == "binary", f"the binary subprotocol is echoed back (got {protocol!r})")
                ws.send_bytes(b"client-hello")
                echoed = ws.receive_bytes()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"an authenticated /websockify session failed: {type(exc).__name__}: {exc}")

        check(echoed is not None and echoed.startswith(b"client-hello"),
              f"client->server bytes reached websockify (got {echoed!r})")
        check(echoed is not None and echoed.endswith(b"|from-server"),
              "server->client bytes came back through the app")

        # --- the upstream is always loopback -------------------------------
        target = websockify_proxy.loopback_websockify_url(port)
        check(target.startswith("ws://127.0.0.1:"), f"the proxy target is loopback (got {target})")
        check("localhost" not in target, "the proxy target cannot be redirected by hostname")
    finally:
        main_module.computer.settings.screen_ws_port = original_port
        state["stop"] = True

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) did not hold")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: /websockify relays binary frames both ways, behind a session")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
